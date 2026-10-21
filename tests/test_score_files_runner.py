"""Tests for the two-phase LLM scorer: phase 1 path-only triage, phase 2
content-aware re-rank, plus the integration pipeline that ties prefilter →
phase 1 → phase 2 together."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import score_files  # noqa: E402


def _make_args(**overrides) -> SimpleNamespace:
    base = dict(
        concurrency=2,
        sample_bytes=4096,
        endpoint="http://localhost:8001",
        model="test-model",
        oversized_skip_bytes=score_files.DEFAULT_OVERSIZED_SKIP_BYTES,
        phase1_batch_size=100,
        phase2_batch_size=10,
        debug=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------
# Phase 1 — path-only batched scorer with thread-pool dispatch
# --------------------------------------------------------------------------

def test_phase1_single_batch_all_scored():
    """One batch, all paths come back parsed."""
    paths = ["src/a.go", "src/b.go", "src/c.go"]
    fake_output = "5|src/a.go|auth handler\n3|src/b.go|util\n2|src/c.go|glue"
    with patch.object(score_files, "run_llm_scorer", return_value=fake_output) as m:
        results = score_files.llm_score_phase1(
            paths=paths, args=_make_args(), repo_analysis=None,
        )
    assert m.call_count == 1
    assert results["src/a.go"]["score"] == 5
    assert results["src/b.go"]["score"] == 3
    assert results["src/c.go"]["score"] == 2


def test_phase1_dispatches_batches_concurrently():
    """Multiple batches are dispatched through the thread pool, not serially.

    With phase1_batch_size=2 and 5 paths, expect 3 batches (2+2+1). With
    concurrency=3, all three should run in parallel — we verify by counting
    that run_llm_scorer was called exactly 3 times and that all 5 paths
    appear in the result map.
    """
    paths = [f"src/{c}.go" for c in "abcde"]
    # Each call returns the sub-batch of the input prompt that we recognize.
    def _fake(prompt, args):
        # Produce one line per path mentioned in the prompt
        out = []
        for p in paths:
            if f"\n{p}\n" in prompt or prompt.endswith(f"\n{p}"):
                out.append(f"4|{p}|prod handler")
        return "\n".join(out)

    with patch.object(score_files, "run_llm_scorer", side_effect=_fake) as m:
        results = score_files.llm_score_phase1(
            paths=paths,
            args=_make_args(phase1_batch_size=2, concurrency=3),
            repo_analysis=None,
        )
    assert m.call_count == 3
    assert set(results) == set(paths)
    assert all(r["score"] == 4 for r in results.values())


def test_phase1_retries_missed_paths_in_sub_batches():
    """If the model drops some paths in a big batch, the retry pass picks
    them up in 25-path sub-batches. Residual misses default to score 3."""
    paths = ["src/a.go", "src/b.go", "src/c.go"]
    # First call (the big batch): only emit 2 of 3
    # Retry sub-batch call: emit the 1 missed path
    call_outputs = iter([
        "5|src/a.go|h\n3|src/b.go|u",  # initial batch — drops c.go
        "2|src/c.go|recovered",         # retry sub-batch
    ])
    with patch.object(score_files, "run_llm_scorer",
                       side_effect=lambda *a, **kw: next(call_outputs)) as m:
        results = score_files.llm_score_phase1(
            paths=paths, args=_make_args(), repo_analysis=None,
        )
    assert m.call_count == 2
    assert results["src/a.go"]["score"] == 5
    assert results["src/b.go"]["score"] == 3
    assert results["src/c.go"]["score"] == 2


def test_phase1_residual_misses_default_to_3():
    """Both the initial batch and the retry pass drop a path → default 3."""
    paths = ["src/a.go", "src/b.go"]
    call_outputs = iter([
        "5|src/a.go|h",  # batch emits only a.go
        "",               # retry emits nothing
    ])
    with patch.object(score_files, "run_llm_scorer",
                       side_effect=lambda *a, **kw: next(call_outputs)):
        results = score_files.llm_score_phase1(
            paths=paths, args=_make_args(), repo_analysis=None,
        )
    assert results["src/a.go"]["score"] == 5
    assert results["src/b.go"]["score"] == 3
    assert results["src/b.go"]["rationale"] == ""


def test_phase1_one_batch_raising_doesnt_abandon_others():
    """If one batch worker raises (e.g. JSONDecodeError from a malformed
    proxy response), other in-flight batches must still be processed and
    the dropped batch's paths must default to score 3."""
    paths = ["src/a.go", "src/b.go", "src/c.go", "src/d.go"]
    # phase1_batch_size=2 → 2 batches: [a,b] and [c,d]
    # Make the [a,b] batch raise; the [c,d] batch returns clean output.
    call_count = {"n": 0}
    def _fake(prompt, args):
        call_count["n"] += 1
        if "src/a.go" in prompt:
            raise ValueError("simulated proxy returning HTML on overload")
        return "5|src/c.go|h\n4|src/d.go|h"
    with patch.object(score_files, "run_llm_scorer", side_effect=_fake):
        results = score_files.llm_score_phase1(
            paths=paths,
            args=_make_args(phase1_batch_size=2, concurrency=2),
            repo_analysis=None,
        )
    # The clean batch's paths must be scored from the model
    assert results["src/c.go"]["score"] == 5
    assert results["src/d.go"]["score"] == 4
    # The failing batch's paths attempt retry; if retry also raises (it
    # will — the fake raises any time src/a.go is in the prompt), they
    # default to score 3 with empty rationale.
    assert results["src/a.go"]["score"] == 3
    assert results["src/b.go"]["score"] == 3
    assert results["src/a.go"]["rationale"] == ""
    assert results["src/b.go"]["rationale"] == ""


# --------------------------------------------------------------------------
# Phase 2 — content-aware batched re-rank with thread-pool dispatch
# --------------------------------------------------------------------------

def _write_files(repo_root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        full = repo_root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)


def test_phase2_single_batch_rerank(tmp_path):
    """Files with score >= 2 in phase 1 are passed to phase 2 with content
    samples, and the LLM's re-scored values overwrite phase 1."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_files(repo_root, {
        "src/a.go": "package main\nfunc handler() {}\n",
        "src/b.go": "package main\nfunc helper() {}\n",
    })
    sizes = {p: (repo_root / p).stat().st_size for p in ("src/a.go", "src/b.go")}
    phase1 = {
        "src/a.go": {"score": 4, "rationale": "looks like handler"},
        "src/b.go": {"score": 3, "rationale": "internal"},
    }
    fake_output = "5|src/a.go|confirmed handler\n2|src/b.go|just glue"
    with patch.object(score_files, "run_llm_scorer", return_value=fake_output) as m:
        results = score_files.llm_score_phase2(
            phase1_results=phase1,
            sizes_by_path=sizes,
            repo_root=repo_root,
            args=_make_args(phase2_batch_size=10),
            repo_analysis=None,
        )
    assert m.call_count == 1
    assert results["src/a.go"]["score"] == 5
    assert results["src/b.go"]["score"] == 2


def test_phase2_skips_files_below_threshold(tmp_path):
    """Files scoring 1 in phase 1 are NOT included in phase 2 input."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_files(repo_root, {"src/a.go": "x", "src/b.go": "y"})
    sizes = {p: (repo_root / p).stat().st_size for p in ("src/a.go", "src/b.go")}
    phase1 = {
        "src/a.go": {"score": 2, "rationale": ""},
        "src/b.go": {"score": 1, "rationale": ""},  # below threshold
    }
    captured: list[str] = []
    def _fake(prompt, args):
        captured.append(prompt)
        return "3|src/a.go|reranked"
    with patch.object(score_files, "run_llm_scorer", side_effect=_fake):
        results = score_files.llm_score_phase2(
            phase1_results=phase1,
            sizes_by_path=sizes,
            repo_root=repo_root,
            args=_make_args(),
            repo_analysis=None,
        )
    # b.go must not appear in either the prompt or the results
    assert all("src/b.go" not in p for p in captured)
    assert "src/b.go" not in results
    assert results["src/a.go"]["score"] == 3


def test_phase2_one_batch_raising_doesnt_abandon_others(tmp_path):
    """If one batch worker raises (e.g. JSONDecodeError from a malformed
    proxy response), other in-flight batches must still be processed.
    Mirrors the phase 1 failure-isolation test."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    files = {f"src/{c}.go": f"package {c}\n" for c in "abcd"}
    # llm_score_phase2 sorts candidates alphabetically, so with phase2_batch_size=2
    # the batches are [a,b] and [c,d] — the failing-on-a.go fake takes both a and b down.
    _write_files(repo_root, files)
    sizes = {p: (repo_root / p).stat().st_size for p in files}
    phase1 = {p: {"score": 3, "rationale": ""} for p in files}
    def _fake(prompt, args):
        if "src/a.go" in prompt:
            raise ValueError("simulated proxy returning HTML on overload")
        # only c and d here (b shares the failing batch with a)
        out = []
        for p in ("src/c.go", "src/d.go"):
            if f"### {p}" in prompt:
                out.append(f"4|{p}|reranked")
        return "\n".join(out)
    with patch.object(score_files, "run_llm_scorer", side_effect=_fake):
        results = score_files.llm_score_phase2(
            phase1_results=phase1,
            sizes_by_path=sizes,
            repo_root=repo_root,
            args=_make_args(phase2_batch_size=2, concurrency=2),
            repo_analysis=None,
        )
    # The clean batch's paths must be scored from the model
    assert results["src/c.go"]["score"] == 4
    assert results["src/d.go"]["score"] == 4
    # The failing batch's paths are dropped from the result map (caller
    # falls back to phase 1 scores in the integration driver).
    assert "src/a.go" not in results
    assert "src/b.go" not in results


def test_phase2_retry_sub_batches_drops_instead_of_one_megabatch(tmp_path):
    """When the initial dispatch drops files (batches return empty), the
    retry pass MUST sub-batch them at the configured phase2_batch_size,
    not blast all drops in one mega-batch. Regression target: with the
    8 KB per-file content cap, 60 dropped files in one retry batch hits
    HTTP 400 from the LLM proxy (480 KB content > context window).

    Setup: 6 files, batch_size=2 → 3 initial batches that all return
    empty. Retry must dispatch 3 sub-batches of 2 (not one of 6)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    files = {f"src/{c}.go": f"package {c}\n" for c in "abcdef"}
    _write_files(repo_root, files)
    sizes = {p: (repo_root / p).stat().st_size for p in files}
    phase1 = {p: {"score": 3, "rationale": ""} for p in files}

    call_count = {"n": 0}
    seen_batch_sizes: list[int] = []

    def _fake(prompt, args):
        call_count["n"] += 1
        # Count file headers in the prompt to infer batch size
        seen_batch_sizes.append(prompt.count("\n### "))
        # First 3 calls (the initial pass) return empty → all drop.
        # Retry-pass calls (4+) return scores so the retry recovers them.
        if call_count["n"] <= 3:
            return ""
        out = []
        for p in files:
            if f"### {p}" in prompt:
                out.append(f"4|{p}|recovered on retry")
        return "\n".join(out)

    with patch.object(score_files, "run_llm_scorer", side_effect=_fake):
        results = score_files.llm_score_phase2(
            phase1_results=phase1,
            sizes_by_path=sizes,
            repo_root=repo_root,
            args=_make_args(phase2_batch_size=2, concurrency=2),
            repo_analysis=None,
        )

    # 3 initial batches of 2 + 3 retry sub-batches of 2 = 6 LLM calls total.
    assert call_count["n"] == 6, f"expected 6 calls, got {call_count['n']}"
    # No call ever exceeds batch_size — the megabatch bug is gone.
    assert max(seen_batch_sizes) == 2, (
        f"retry must sub-batch at phase2_batch_size=2; "
        f"saw batch sizes {seen_batch_sizes}"
    )
    # All 6 files recovered through retry.
    assert len(results) == 6
    assert all(r["rationale"] == "recovered on retry" for r in results.values())


def test_phase2_retry_sub_batch_failure_does_not_lose_other_sub_batches(tmp_path):
    """One sub-batch failing on retry must not abort the other sub-batches.
    Mirrors the initial-pass failure-isolation guarantee."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    files = {f"src/{c}.go": f"package {c}\n" for c in "abcd"}
    _write_files(repo_root, files)
    sizes = {p: (repo_root / p).stat().st_size for p in files}
    phase1 = {p: {"score": 3, "rationale": ""} for p in files}

    call_count = {"n": 0}

    def _fake(prompt, args):
        call_count["n"] += 1
        # Initial pass (calls 1-2): drop everything.
        if call_count["n"] <= 2:
            return ""
        # Retry pass: the sub-batch containing src/a.go fails; the other recovers.
        if "src/a.go" in prompt:
            raise ValueError("simulated proxy 400 on this sub-batch")
        out = []
        for p in files:
            if f"### {p}" in prompt:
                out.append(f"4|{p}|recovered")
        return "\n".join(out)

    with patch.object(score_files, "run_llm_scorer", side_effect=_fake):
        results = score_files.llm_score_phase2(
            phase1_results=phase1,
            sizes_by_path=sizes,
            repo_root=repo_root,
            args=_make_args(phase2_batch_size=2, concurrency=2),
            repo_analysis=None,
        )

    # The clean retry sub-batch's paths recover.
    assert results["src/c.go"]["score"] == 4
    assert results["src/d.go"]["score"] == 4
    # The failing retry sub-batch's paths stay missing — caller will fall
    # back to phase 1 scores via the synthesizer.
    assert "src/a.go" not in results
    assert "src/b.go" not in results


def test_phase2_dispatches_batches_concurrently(tmp_path):
    """With phase2_batch_size=2 and 5 candidates, expect 3 batches dispatched
    through the thread pool."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    files = {f"src/{c}.go": f"package {c}\n" for c in "abcde"}
    _write_files(repo_root, files)
    sizes = {p: (repo_root / p).stat().st_size for p in files}
    phase1 = {p: {"score": 3, "rationale": ""} for p in files}
    def _fake(prompt, args):
        out = []
        for p in files:
            if f"### {p}" in prompt:
                out.append(f"4|{p}|reranked")
        return "\n".join(out)
    with patch.object(score_files, "run_llm_scorer", side_effect=_fake) as m:
        results = score_files.llm_score_phase2(
            phase1_results=phase1,
            sizes_by_path=sizes,
            repo_root=repo_root,
            args=_make_args(phase2_batch_size=2, concurrency=3),
            repo_analysis=None,
        )
    assert m.call_count == 3
    assert set(results) == set(files)
    assert all(r["score"] == 4 for r in results.values())


# --------------------------------------------------------------------------
# Integration — prefilter → phase1 → phase2 pipeline
# --------------------------------------------------------------------------

def _make_inventory_db(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    """Build a small repo + inventory DB with three files spanning the
    prefilter/phase1/phase2 paths."""
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "tests").mkdir()
    (repo_root / "src" / "auth.go").write_text("package auth\nfunc Login() {}\n")
    (repo_root / "src" / "util.go").write_text("package util\nfunc Add() {}\n")
    (repo_root / "tests" / "auth_test.go").write_text("package auth_test\n")

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE files (
            id INTEGER PRIMARY KEY, path TEXT, extension TEXT, category TEXT,
            is_binary INTEGER, size_bytes INTEGER, excluded INTEGER,
            exclusion_reason TEXT, tags_json TEXT
        )
    """)
    for fid, path in enumerate([
        "src/auth.go", "src/util.go", "tests/auth_test.go",
    ], start=1):
        size = (repo_root / path).stat().st_size
        conn.execute(
            "INSERT INTO files VALUES (?, ?, '.go', 'code', 0, ?, 0, NULL, '[]')",
            (fid, path, size),
        )
    conn.commit()
    return conn, repo_root


def test_integration_prefilter_then_phase1_then_phase2(tmp_path):
    """End-to-end: prefilter routes test file to score 1; phase 1 triages
    src/* with phase1 batch; phase 2 re-ranks the survivor with content."""
    conn, repo_root = _make_inventory_db(tmp_path)
    profile = {"cold_path_prefixes": ["tests/"]}

    # Phase 1 returns: auth.go=4 (qualifies for phase 2), util.go=2 (also qualifies)
    # Phase 2 returns: auth.go=5 (promoted), util.go=2 (unchanged)
    call_outputs = iter([
        # phase 1 batch
        "4|src/auth.go|handler\n2|src/util.go|helper",
        # phase 2 batch (both qualify under threshold=2)
        "5|src/auth.go|confirmed auth\n2|src/util.go|confirmed glue",
    ])
    with patch.object(score_files, "run_llm_scorer",
                       side_effect=lambda *a, **kw: next(call_outputs)) as m:
        results = score_files.run_two_phase_llm_scorer(
            conn, tmp_path, repo_root,
            args=_make_args(),
            profile=profile,
            repo_analysis=None,
        )
    assert m.call_count == 2  # one phase 1 batch + one phase 2 batch

    assert results["tests/auth_test.go"].score == 1
    assert results["tests/auth_test.go"].scored_by == "prefilter"

    assert results["src/auth.go"].score == 5  # phase 2 promoted from 4 to 5
    assert results["src/auth.go"].scored_by == "llm-content"

    assert results["src/util.go"].score == 2  # phase 2 confirmed
    assert results["src/util.go"].scored_by == "llm-content"


def test_integration_phase1_score_1_skips_phase2(tmp_path):
    """Files scoring 1 in phase 1 do not enter phase 2; they retain their
    phase-1 score with scored_by=llm-path."""
    conn, repo_root = _make_inventory_db(tmp_path)
    profile = {"cold_path_prefixes": []}  # no prefilter; phase 1 sees everything

    call_outputs = iter([
        # phase 1: util.go scored 1 → must NOT appear in phase 2 input
        "5|src/auth.go|h\n1|src/util.go|trivial\n1|tests/auth_test.go|test",
        # phase 2: only auth.go (score 5 >= threshold 2)
        "5|src/auth.go|confirmed",
    ])
    with patch.object(score_files, "run_llm_scorer",
                       side_effect=lambda *a, **kw: next(call_outputs)) as m:
        results = score_files.run_two_phase_llm_scorer(
            conn, tmp_path, repo_root,
            args=_make_args(),
            profile=profile,
            repo_analysis=None,
        )
    assert m.call_count == 2
    assert results["src/util.go"].score == 1
    assert results["src/util.go"].scored_by == "llm-path"
    assert results["tests/auth_test.go"].score == 1
    assert results["tests/auth_test.go"].scored_by == "llm-path"


def test_integration_oversized_skips_both_phases(tmp_path):
    """Files above oversized cap get score=3/scored_by=oversized-skip and
    never reach the LLM."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "huge.py").write_text("x = 1\n")
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE files (
            id INTEGER PRIMARY KEY, path TEXT, extension TEXT, category TEXT,
            is_binary INTEGER, size_bytes INTEGER, excluded INTEGER,
            exclusion_reason TEXT, tags_json TEXT
        )
    """)
    conn.execute(
        "INSERT INTO files VALUES (1, 'huge.py', '.py', 'code', 0, 5000000, 0, NULL, '[]')"
    )
    conn.commit()

    with patch.object(score_files, "run_llm_scorer") as m:
        results = score_files.run_two_phase_llm_scorer(
            conn, tmp_path, repo_root,
            args=_make_args(),
            profile={},
            repo_analysis=None,
        )
    assert m.call_count == 0
    assert results["huge.py"].score == 3
    assert results["huge.py"].scored_by == "oversized-skip"


# --------------------------------------------------------------------------
# Synthesizer rules — phase-1-rationale fallback (Fix A) and
# production-source demotion floor (Fix B)
# --------------------------------------------------------------------------

def test_synthesizer_falls_back_to_phase1_rationale_when_phase2_empty(tmp_path):
    """If phase 2 returns a score but no rationale, the synthesizer keeps
    the phase 1 rationale instead of overwriting with empty string. Without
    this, P3+ files routinely lose their explanation when the LLM omits the
    rationale field."""
    conn, repo_root = _make_inventory_db(tmp_path)
    profile = {"cold_path_prefixes": ["tests/"]}

    call_outputs = iter([
        # phase 1: both src files get strong rationales
        "4|src/auth.go|HTTP login handler from path\n3|src/util.go|likely glue from path",
        # phase 2: both rated 4, but rationale OMITTED (just `score|path`)
        "4|src/auth.go\n4|src/util.go",
    ])
    with patch.object(score_files, "run_llm_scorer",
                       side_effect=lambda *a, **kw: next(call_outputs)):
        results = score_files.run_two_phase_llm_scorer(
            conn, tmp_path, repo_root,
            args=_make_args(),
            profile=profile,
            repo_analysis=None,
        )

    # Phase 2 score wins, but phase 1 rationale survives the empty handoff.
    assert results["src/auth.go"].score == 4
    assert results["src/auth.go"].rationale == "HTTP login handler from path"
    assert results["src/util.go"].score == 4
    assert results["src/util.go"].rationale == "likely glue from path"
    # Signals also reflect the fallback rationale, not empty string.
    assert results["src/auth.go"].signals["rationale"] == "HTTP login handler from path"


def test_synthesizer_prefers_phase2_rationale_when_present(tmp_path):
    """Sanity check: when phase 2 DOES emit a rationale, it wins over
    phase 1's rationale (this is the normal/intended path)."""
    conn, repo_root = _make_inventory_db(tmp_path)
    profile = {"cold_path_prefixes": ["tests/"]}

    call_outputs = iter([
        "4|src/auth.go|path-only guess\n3|src/util.go|path-only guess",
        "4|src/auth.go|content confirms login handler\n3|src/util.go|content confirms helper",
    ])
    with patch.object(score_files, "run_llm_scorer",
                       side_effect=lambda *a, **kw: next(call_outputs)):
        results = score_files.run_two_phase_llm_scorer(
            conn, tmp_path, repo_root,
            args=_make_args(),
            profile=profile,
            repo_analysis=None,
        )

    assert results["src/auth.go"].rationale == "content confirms login handler"
    assert results["src/util.go"].rationale == "content confirms helper"


def test_synthesizer_floors_production_source_at_3_when_phase1_strong(tmp_path):
    """Production-source files (recognized code extension) that phase 1
    ranked >= 3 cannot be demoted below 3 by phase 2's content sample.
    Catches the observed pathological pattern where Django views.py /
    models.py went from P5 to P1 because phase 2 didn't see the dangerous
    code in its 4 KB sample window."""
    conn, repo_root = _make_inventory_db(tmp_path)
    profile = {"cold_path_prefixes": ["tests/"]}

    call_outputs = iter([
        # phase 1: both src files rated 5 (path heuristics say real source)
        "5|src/auth.go|views file\n5|src/util.go|models file",
        # phase 2: LLM demotes BOTH to 1 based on 4 KB sample
        "1|src/auth.go|looks like trivial constants\n1|src/util.go|just type defs",
    ])
    with patch.object(score_files, "run_llm_scorer",
                       side_effect=lambda *a, **kw: next(call_outputs)):
        results = score_files.run_two_phase_llm_scorer(
            conn, tmp_path, repo_root,
            args=_make_args(),
            profile=profile,
            repo_analysis=None,
        )

    # Floor kicks in: phase 1 was 5 (>= 3), .go is a production extension,
    # phase 2 said 1 (< 3) → final clamps to 3.
    assert results["src/auth.go"].score == 3
    assert results["src/auth.go"].signals.get("floor_applied") is True
    assert results["src/auth.go"].signals.get("phase2_raw_score") == 1
    assert "floor=3" in results["src/auth.go"].rationale
    assert results["src/util.go"].score == 3
    assert results["src/util.go"].signals.get("floor_applied") is True


def test_synthesizer_floor_does_not_apply_when_phase1_below_3(tmp_path):
    """The floor is keyed off phase 1 >= 3 — if phase 1 path heuristics
    already said 'low risk' (< 3), phase 2 is free to confirm low."""
    conn, repo_root = _make_inventory_db(tmp_path)
    profile = {"cold_path_prefixes": ["tests/"]}

    call_outputs = iter([
        # phase 1: low ratings (path looks unimportant)
        "2|src/auth.go|seems trivial\n2|src/util.go|seems trivial",
        # phase 2: confirms low
        "1|src/auth.go|confirmed trivial\n1|src/util.go|confirmed trivial",
    ])
    with patch.object(score_files, "run_llm_scorer",
                       side_effect=lambda *a, **kw: next(call_outputs)):
        results = score_files.run_two_phase_llm_scorer(
            conn, tmp_path, repo_root,
            args=_make_args(),
            profile=profile,
            repo_analysis=None,
        )

    # No floor — phase 1 was below 3, so phase 2's demotion stands.
    assert results["src/auth.go"].score == 1
    assert "floor_applied" not in results["src/auth.go"].signals
    assert results["src/util.go"].score == 1


def test_synthesizer_floor_does_not_apply_to_non_code_extension(tmp_path):
    """The floor only applies to recognized code extensions. A .json or
    .yaml file that phase 1 over-promoted should still be free to demote
    below 3 — config files don't get the production-source floor."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "config.yaml").write_text("foo: bar\n")
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE files (
            id INTEGER PRIMARY KEY, path TEXT, extension TEXT, category TEXT,
            is_binary INTEGER, size_bytes INTEGER, excluded INTEGER,
            exclusion_reason TEXT, tags_json TEXT
        )
    """)
    size = (repo_root / "config.yaml").stat().st_size
    conn.execute(
        "INSERT INTO files VALUES (1, 'config.yaml', '.yaml', 'code', 0, ?, 0, NULL, '[]')",
        (size,),
    )
    conn.commit()
    profile = {"cold_path_prefixes": []}

    call_outputs = iter([
        "4|config.yaml|looks like a config",   # phase 1 over-promoted
        "1|config.yaml|empty key:value",       # phase 2 demotes hard
    ])
    with patch.object(score_files, "run_llm_scorer",
                       side_effect=lambda *a, **kw: next(call_outputs)):
        results = score_files.run_two_phase_llm_scorer(
            conn, tmp_path, repo_root,
            args=_make_args(),
            profile=profile,
            repo_analysis=None,
        )

    # No floor — .yaml is not a production code extension.
    assert results["config.yaml"].score == 1
    assert "floor_applied" not in results["config.yaml"].signals


def test_phase2_per_file_content_cap_is_8k_not_4k(tmp_path):
    """Per-file content cap doubled from 4 KB to 8 KB. Without this, content
    past byte 4096 was always truncated even when read_content_sample had
    pulled it (head/middle/tail or whole-file). Regression target: dangerous
    code that lives past the head slice (e.g. read_config.py:31 yaml.load
    fallback in an except block past the imports)."""
    assert score_files.PHASE2_PER_FILE_CONTENT_BYTES == 8192

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # 7 KB file — past the old 4 KB truncation, under the new 8 KB cap.
    # Distinct markers at known byte offsets so we can prove what survived.
    body = ("HEAD_REGION_" + "a" * 4000
            + "MARKER_AT_4012_PAST_OLD_CAP"
            + "b" * 2000
            + "MARKER_AT_6039_NEAR_END")
    (repo_root / "big.go").write_text(body)
    sizes = {"big.go": len(body)}
    phase1 = {"big.go": {"score": 4, "rationale": "looks important"}}

    captured_prompts: list[str] = []
    def _fake(prompt, args):
        captured_prompts.append(prompt)
        return "4|big.go|reranked"

    # sample_bytes=8192 ensures read_content_sample returns the whole 7 KB
    # file in one piece (no head/middle/tail slicing that would interleave
    # newlines and confuse the marker assertion). The cap-under-test then
    # operates on that whole-file content.
    with patch.object(score_files, "run_llm_scorer", side_effect=_fake):
        score_files.llm_score_phase2(
            phase1_results=phase1,
            sizes_by_path=sizes,
            repo_root=repo_root,
            args=_make_args(sample_bytes=8192),
            repo_analysis=None,
        )

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    # Both markers past byte 4096 must survive the 8 KB cap.
    assert "MARKER_AT_4012_PAST_OLD_CAP" in prompt
    assert "MARKER_AT_6039_NEAR_END" in prompt
