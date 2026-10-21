"""Unit tests for decompose.plan_slices / should_decompose / expand_run."""
from __future__ import annotations

import common
import decompose


# ---- plan_slices ----

def test_no_function_boundaries_returns_whole_file():
    assert decompose.plan_slices(500, [], 6000, budget_tokens=12000, max_slices=8) == [(1, 500)]


def test_tiles_with_no_gaps_or_overlaps():
    fns = [1, 1000, 2000, 3000]
    slices = decompose.plan_slices(4000, fns, 40000, budget_tokens=12000, max_slices=8)
    assert slices[0][0] == 1
    assert slices[-1][1] == 4000
    for (s1, e1), (s2, e2) in zip(slices, slices[1:]):
        assert s2 == e1 + 1
    assert len(slices) >= 3


def test_boundaries_snap_to_function_starts():
    fns = [1, 1500, 3000]
    slices = decompose.plan_slices(4000, fns, 48000, budget_tokens=12000, max_slices=8)
    starts = {s for s, _ in slices}
    assert starts - {1} <= set(fns)


def test_oversized_single_function_is_its_own_slice():
    fns = [1, 50]
    slices = decompose.plan_slices(5000, fns, 50000, budget_tokens=12000, max_slices=8)
    assert slices[-1][1] == 5000


def test_max_slices_cap_is_enforced():
    fns = list(range(1, 2000, 100))  # 20 functions
    slices = decompose.plan_slices(2000, fns, 200000, budget_tokens=1000, max_slices=4)
    assert len(slices) <= 4
    assert slices[0][0] == 1 and slices[-1][1] == 2000


def test_plan_slices_merges_degenerate_trailing_slice():
    # Boundaries that greedily orphan a tiny 5-line tail (1996-2000); it must be
    # merged into its predecessor rather than burning a whole pass on 5 lines.
    slices = decompose.plan_slices(2000, [1, 1000, 1996], 24000, budget_tokens=12000, max_slices=8)
    assert slices == [(1, 999), (1000, 2000)]


# ---- decorator_aware_starts ----

def test_decorator_aware_starts_backs_up_past_a_decorator():
    lines = ["def a():", "    pass", "", "@login_required", "def b():", "    pass"]
    # def b at line 5; its @login_required is line 4 → boundary moves to 4
    assert decompose.decorator_aware_starts(lines, [1, 5]) == [1, 4]


def test_decorator_aware_starts_backs_up_past_stacked_decorators():
    lines = ["@require_POST", "@login_required", "def b():", "    pass"]
    assert decompose.decorator_aware_starts(lines, [3]) == [1]


def test_decorator_aware_starts_unchanged_without_decorator():
    lines = ["x = 1", "def b():", "    pass"]
    assert decompose.decorator_aware_starts(lines, [2]) == [2]


# ---- should_decompose ----

def test_should_decompose_true_when_all_thresholds_met():
    assert decompose.should_decompose(
        file_tokens=20000, fn_count=5, priority=4, cpg_available=True
    ) is True


def test_should_decompose_false_for_small_file():
    assert decompose.should_decompose(
        file_tokens=5000, fn_count=5, priority=5, cpg_available=True
    ) is False


def test_should_decompose_false_without_cpg():
    assert decompose.should_decompose(
        file_tokens=50000, fn_count=20, priority=5, cpg_available=False
    ) is False


def test_should_decompose_false_low_priority_or_few_functions():
    assert decompose.should_decompose(
        file_tokens=50000, fn_count=20, priority=2, cpg_available=True
    ) is False
    assert decompose.should_decompose(
        file_tokens=50000, fn_count=1, priority=5, cpg_available=True
    ) is False


# ---- expand_run ----

def _seed_run(run_dir, *, path, lines, priority):
    with common.connect_db(run_dir) as conn:
        now = common.utc_now()
        cur = conn.execute(
            "INSERT INTO files (path, rel_dir, extension, size_bytes, is_binary, "
            "category, tags_json, created_at, updated_at) "
            "VALUES (?,?,?,?,0,?,?,?,?)",
            (path, "x", ".py", lines * 40, "first_party", "[]", now, now),
        )
        fid = cur.lastrowid
        conn.execute(
            "INSERT INTO tasks (file_id, priority, status, created_at, updated_at) "
            "VALUES (?,?,'pending',?,?)",
            (fid, priority, now, now),
        )
    target = run_dir.parent / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(f"line {i}" for i in range(lines)))


class _FakeCpg:
    def __init__(self, endpoint, rows):
        self.rows = rows

    def query(self, scala):
        return self.rows


def test_expand_run_creates_region_subtasks(tmp_path, monkeypatch):
    run_dir = tmp_path / ".rvh-runs"
    run_dir.mkdir()
    _seed_run(run_dir, path="big.py", lines=20000, priority=4)
    rows = [{"file": "big.py", "line": l, "name": f"f{l}"} for l in (1, 5000, 10000, 15000)]
    monkeypatch.setattr(decompose, "CpgClient", lambda ep, **kw: _FakeCpg(ep, rows))

    counts = decompose.expand_run(run_dir, "http://cpg")
    assert counts["files"] == 1
    assert counts["slices"] >= 2

    with common.connect_db(run_dir) as conn:
        subs = conn.execute(
            "SELECT anchor_start_line, anchor_end_line, status, parent_task_id FROM tasks "
            "WHERE anchor_start_line IS NOT NULL ORDER BY anchor_start_line"
        ).fetchall()
        parent = conn.execute(
            "SELECT id, status FROM tasks WHERE anchor_start_line IS NULL"
        ).fetchone()
    assert len(subs) == counts["slices"]
    assert subs[0]["anchor_start_line"] == 1
    assert all(s["status"] == "pending" for s in subs)
    assert all(s["parent_task_id"] == parent["id"] for s in subs)  # ordering anchor
    assert parent["status"] == "decomposed"


def test_expand_run_is_idempotent(tmp_path, monkeypatch):
    run_dir = tmp_path / ".rvh-runs"
    run_dir.mkdir()
    _seed_run(run_dir, path="big.py", lines=20000, priority=4)
    rows = [{"file": "big.py", "line": l, "name": f"f{l}"} for l in (1, 5000, 10000, 15000)]
    monkeypatch.setattr(decompose, "CpgClient", lambda ep, **kw: _FakeCpg(ep, rows))
    decompose.expand_run(run_dir, "http://cpg")
    second = decompose.expand_run(run_dir, "http://cpg")
    assert second["files"] == 0 and second["slices"] == 0


def test_expand_run_noop_without_endpoint(tmp_path):
    run_dir = tmp_path / ".rvh-runs"
    run_dir.mkdir()
    _seed_run(run_dir, path="big.py", lines=20000, priority=4)
    assert decompose.expand_run(run_dir, "") == {"files": 0, "slices": 0}


def test_expand_run_skips_one_bad_file_without_aborting(tmp_path, monkeypatch):
    """A single file whose CPG lookup raises (e.g. a path the validator rejects)
    must be skipped, not abort the whole pass — the other file still decomposes."""
    run_dir = tmp_path / ".rvh-runs"
    run_dir.mkdir()
    _seed_run(run_dir, path="bad@1.2.0.js", lines=20000, priority=4)
    _seed_run(run_dir, path="big.py", lines=20000, priority=4)

    def fake_method_lines(client, path):
        if path == "bad@1.2.0.js":
            raise ValueError("invalid file path")
        return [1, 5000, 10000, 15000]

    monkeypatch.setattr(decompose, "_method_lines", fake_method_lines)
    monkeypatch.setattr(decompose, "CpgClient", lambda ep, **kw: _FakeCpg(ep, []))

    counts = decompose.expand_run(run_dir, "http://cpg")  # must NOT raise
    assert counts["files"] == 1  # only big.py decomposed; bad@ skipped
