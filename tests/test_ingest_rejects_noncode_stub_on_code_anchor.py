"""Ingest-time guard against the step-0 non-code stub on code anchors.

Background (2026-04-21, coriolix-3.0.1 run): workers with thinking
enabled were running a full real investigation on `.py` / `.js` files,
correctly concluding no-finding, and then writing the step-0 boilerplate
to disk ("Non-code anchor — out of scope for this prompt"). That stub's
Evidence section is "N/A — anchor type out of scope", which masks the
sink-enumeration work the worker actually did. Downstream couldn't tell
the difference between a shallow miss and a rigorous no-finding.

The guard in ``ingest_report`` rejects the stub whenever the anchor
extension is NOT in the non-code set, so the caller's existing
exception path requeues the task for a retry under the fixed prompt.

These tests run against the real ``scripts.worker.ingest_report``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from common import connect_db, init_db
from worker import ingest_report


_NOW = "2026-04-21T00:00:00+00:00"

# Verbatim from prompts/*/worker.md's step-0 block. If the prompt ever
# drifts, these tests still verify the guard fires on the historical
# shape — which is exactly the attestation we want from the guard.
_STUB_REPORT_BODY = """---
verdict: "no-finding"
finding_type: "none"
severity: "info"
cwe: ""
title: "Non-code anchor — out of scope for this prompt"
dynamic_evidence: false
---

# Summary

Anchor file is a non-code file (configuration, documentation, build manifest, or data). The current worker prompt is scoped to code-bug analysis (memory safety, injection, deserialization, authorization-logic flaws, etc.) and cannot review non-executable files. Misconfiguration / secrets-disclosure findings would require a different review prompt.

# Evidence

N/A — anchor type out of scope.
"""


def _make_run_layout(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "repo"
    run_dir = repo_root / ".rvh-runs"
    (run_dir / "reports").mkdir(parents=True, exist_ok=True)
    return repo_root, run_dir


def _seed_task(run_dir: Path, task_id: int, anchor_rel: str, ext: str) -> None:
    init_db(run_dir)
    with connect_db(run_dir) as conn:
        conn.execute(
            "INSERT INTO files (path, rel_dir, extension, size_bytes, is_binary, "
            "category, tags_json, excluded, created_at, updated_at) "
            "VALUES (?, '', ?, 100, 0, 'code', '[]', 0, ?, ?)",
            (anchor_rel, ext, _NOW, _NOW),
        )
        row = conn.execute(
            "SELECT id FROM files WHERE path = ?", (anchor_rel,)
        ).fetchone()
        conn.execute(
            "INSERT INTO tasks (id, file_id, priority, status, created_at, updated_at) "
            "VALUES (?, ?, 5, 'completed', ?, ?)",
            (task_id, row["id"], _NOW, _NOW),
        )


def test_ingest_rejects_noncode_stub_on_python_anchor(tmp_path):
    """The canonical failure mode: stub shipped for a .py anchor -> raise."""
    repo_root, run_dir = _make_run_layout(tmp_path)
    anchor = repo_root / "app" / "views.py"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text("def index(request): return 'ok'\n", encoding="utf-8")
    _seed_task(run_dir, task_id=1, anchor_rel="app/views.py", ext="py")

    report_path = run_dir / "reports" / "task-1.md"
    report_path.write_text(_STUB_REPORT_BODY, encoding="utf-8")

    with connect_db(run_dir) as conn:
        with pytest.raises(ValueError, match="non-code early-exit stub"):
            ingest_report(
                conn,
                run_id="test",
                report_path=report_path,
                now=_NOW,
                task_id=1,
                anchor_file="app/views.py",
                run_dir=run_dir,
            )

        # DB must be clean — the raise happens before any INSERT.
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM worker_reports"
        ).fetchone()
        assert rows["c"] == 0


def test_ingest_rejects_noncode_stub_on_javascript_anchor(tmp_path):
    repo_root, run_dir = _make_run_layout(tmp_path)
    anchor = repo_root / "chart.js"
    anchor.write_text("export function draw() {}\n", encoding="utf-8")
    _seed_task(run_dir, task_id=2, anchor_rel="chart.js", ext="js")

    report_path = run_dir / "reports" / "task-2.md"
    report_path.write_text(_STUB_REPORT_BODY, encoding="utf-8")

    with connect_db(run_dir) as conn:
        with pytest.raises(ValueError, match="non-code early-exit stub"):
            ingest_report(
                conn,
                run_id="test",
                report_path=report_path,
                now=_NOW,
                task_id=2,
                anchor_file="chart.js",
                run_dir=run_dir,
            )


def test_ingest_accepts_noncode_stub_on_html_anchor(tmp_path):
    """Legit non-code early-exit: .html anchor -> stub is correct output."""
    repo_root, run_dir = _make_run_layout(tmp_path)
    anchor = repo_root / "templates" / "login.html"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text("<html></html>\n", encoding="utf-8")
    _seed_task(run_dir, task_id=3, anchor_rel="templates/login.html", ext="html")

    report_path = run_dir / "reports" / "task-3.md"
    report_path.write_text(_STUB_REPORT_BODY, encoding="utf-8")

    with connect_db(run_dir) as conn:
        verdict, _title, finding_id = ingest_report(
            conn,
            run_id="test",
            report_path=report_path,
            now=_NOW,
            task_id=3,
            anchor_file="templates/login.html",
            run_dir=run_dir,
        )
    assert verdict == "no-finding"
    assert finding_id is None


def test_ingest_accepts_noncode_stub_on_yaml_anchor(tmp_path):
    repo_root, run_dir = _make_run_layout(tmp_path)
    anchor = repo_root / "config.yaml"
    anchor.write_text("key: value\n", encoding="utf-8")
    _seed_task(run_dir, task_id=4, anchor_rel="config.yaml", ext="yaml")

    report_path = run_dir / "reports" / "task-4.md"
    report_path.write_text(_STUB_REPORT_BODY, encoding="utf-8")

    with connect_db(run_dir) as conn:
        verdict, *_ = ingest_report(
            conn,
            run_id="test",
            report_path=report_path,
            now=_NOW,
            task_id=4,
            anchor_file="config.yaml",
            run_dir=run_dir,
        )
    assert verdict == "no-finding"


def test_ingest_accepts_noncode_stub_on_dockerfile_anchor(tmp_path):
    """Dockerfile has no extension — basename match."""
    repo_root, run_dir = _make_run_layout(tmp_path)
    anchor = repo_root / "Dockerfile"
    anchor.write_text("FROM alpine\n", encoding="utf-8")
    _seed_task(run_dir, task_id=5, anchor_rel="Dockerfile", ext="")

    report_path = run_dir / "reports" / "task-5.md"
    report_path.write_text(_STUB_REPORT_BODY, encoding="utf-8")

    with connect_db(run_dir) as conn:
        verdict, *_ = ingest_report(
            conn,
            run_id="test",
            report_path=report_path,
            now=_NOW,
            task_id=5,
            anchor_file="Dockerfile",
            run_dir=run_dir,
        )
    assert verdict == "no-finding"


def test_ingest_accepts_real_no_finding_on_python_anchor(tmp_path):
    """Real no-finding report with a normal title on a .py anchor -> no raise."""
    repo_root, run_dir = _make_run_layout(tmp_path)
    anchor = repo_root / "utils.py"
    anchor.write_text("def safe(): pass\n", encoding="utf-8")
    _seed_task(run_dir, task_id=6, anchor_rel="utils.py", ext="py")

    report_path = run_dir / "reports" / "task-6.md"
    report_path.write_text(
        """---
verdict: "no-finding"
finding_type: "none"
severity: "info"
title: "No surviving sinks in utility helpers"
---

# Summary

All filesystem sinks use `os.path.basename` before I/O, eliminating path traversal.

# Evidence

- Shell / command execution: 0 matches
- Filesystem paths: 1 match at utils.py:3 (os.path.basename)
""",
        encoding="utf-8",
    )

    with connect_db(run_dir) as conn:
        verdict, *_ = ingest_report(
            conn,
            run_id="test",
            report_path=report_path,
            now=_NOW,
            task_id=6,
            anchor_file="utils.py",
            run_dir=run_dir,
        )
    assert verdict == "no-finding"


def test_ingest_rejects_stub_summary_even_with_drifted_title(tmp_path):
    """Defense-in-depth: catch the stub via its summary body too."""
    repo_root, run_dir = _make_run_layout(tmp_path)
    anchor = repo_root / "svc.py"
    anchor.write_text("x = 1\n", encoding="utf-8")
    _seed_task(run_dir, task_id=8, anchor_rel="svc.py", ext="py")

    report_path = run_dir / "reports" / "task-8.md"
    # Title changed, summary body still diagnostic
    report_path.write_text(
        """---
verdict: "no-finding"
finding_type: "none"
severity: "info"
title: "Out of scope for this reviewer"
---

Anchor file is a non-code file (configuration, documentation, build manifest, or data). The current worker prompt is scoped to code-bug analysis and cannot review non-executable files.
""",
        encoding="utf-8",
    )

    with connect_db(run_dir) as conn:
        with pytest.raises(ValueError, match="non-code early-exit"):
            ingest_report(
                conn,
                run_id="test",
                report_path=report_path,
                now=_NOW,
                task_id=8,
                anchor_file="svc.py",
                run_dir=run_dir,
            )


def _seed_run(run_dir: Path) -> None:
    """Structured ingest reads the run row; the markdown tests above don't need it."""
    with connect_db(run_dir) as conn:
        conn.execute(
            "INSERT INTO runs (id, label, repo_root, created_at, updated_at, status) "
            "VALUES ('test', 'test', ?, ?, ?, 'running')",
            (str(run_dir.parent), _NOW, _NOW),
        )


def _structured_stub(task_id: int, anchor_file: str) -> dict:
    """The stub as the step-0 block now asks for it: through recipe__final_output."""
    return {
        "verdict": "no-finding",
        "anchor_file": f"/workspace/{anchor_file}",
        "task_id": task_id,
        "findings": [],
        "summary": (
            "Anchor file is a non-code file (configuration, documentation, build "
            "manifest, or data). The current worker prompt is scoped to code-bug analysis."
        ),
    }


def test_ingest_rejects_structured_noncode_stub_on_code_anchor(tmp_path):
    """Reports arrive as recipe__final_output, so the guard covers that path too."""
    _repo_root, run_dir = _make_run_layout(tmp_path)
    _seed_task(run_dir, task_id=5, anchor_rel="app/views.py", ext="py")
    _seed_run(run_dir)

    with connect_db(run_dir) as conn:
        with pytest.raises(ValueError, match="non-code early-exit stub"):
            ingest_report(
                conn,
                run_id="test",
                report_path=run_dir / "reports" / "task-5.md",
                now=_NOW,
                task_id=5,
                anchor_file="app/views.py",
                run_dir=run_dir,
                structured=_structured_stub(5, "app/views.py"),
            )
        rows = conn.execute("SELECT COUNT(*) AS c FROM worker_reports").fetchone()
        assert rows["c"] == 0


def test_ingest_accepts_structured_noncode_stub_on_yaml_anchor(tmp_path):
    _repo_root, run_dir = _make_run_layout(tmp_path)
    _seed_task(run_dir, task_id=6, anchor_rel="config.yaml", ext="yaml")
    _seed_run(run_dir)

    with connect_db(run_dir) as conn:
        verdict, _title, finding_id = ingest_report(
            conn,
            run_id="test",
            report_path=run_dir / "reports" / "task-6.md",
            now=_NOW,
            task_id=6,
            anchor_file="config.yaml",
            run_dir=run_dir,
            structured=_structured_stub(6, "config.yaml"),
        )
    assert verdict == "no-finding"
    assert finding_id is None
