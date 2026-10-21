"""Unit tests for ingest_worker_report._ingest_structured.

The structured-ingest path takes a validated dict from
extract_final_output, inserts one worker_reports row + N findings rows
(or one stub row for no-finding), and renders a human-readable
task-N.md from the structured object.
"""
from __future__ import annotations

import pathlib
import sqlite3
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
INIT_SCRIPT = REPO_ROOT / "scripts" / "init_run.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from ingest_worker_report import _ingest_structured  # noqa: E402
from common import connect_db  # noqa: E402

_NOW = "2026-04-22T00:00:00+00:00"


@pytest.fixture
def run_dir(tmp_path):
    """Create a minimal run dir with state.db initialized and a task row seeded.

    The ingest script enforces the worker_reports → tasks foreign key, so a
    tasks row must exist for the task_id used before any ingest succeeds.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run = tmp_path / "run"
    subprocess.run(
        [
            sys.executable,
            str(INIT_SCRIPT),
            "--run-dir",
            str(run),
            "--repo",
            str(repo_root),
        ],
        check=True,
        capture_output=True,
    )

    # Seed files and tasks rows for the tests. We'll use task IDs 1, 15, 18 in tests.
    conn = sqlite3.connect(run / "state.db")
    conn.execute("PRAGMA foreign_keys=ON")

    # Create multiple files for different tasks.
    for task_id, file_path in [
        (1, "test.py"),
        (15, "api/permissions.py"),
        (18, "api/views.py"),
    ]:
        conn.execute(
            "INSERT INTO files (path, rel_dir, extension, size_bytes, is_binary, "
            "category, tags_json, excluded, created_at, updated_at) "
            "VALUES (?, '', 'py', 100, 0, 'code', '[]', 0, ?, ?)",
            (file_path, _NOW, _NOW),
        )
        file_id = conn.execute("SELECT id FROM files WHERE path = ?", (file_path,)).fetchone()[0]
        conn.execute(
            "INSERT INTO tasks (id, file_id, priority, status, created_at, updated_at) "
            "VALUES (?, ?, 5, 'completed', ?, ?)",
            (task_id, file_id, _NOW, _NOW),
        )
    conn.commit()
    conn.close()
    return run


def test_no_finding_inserts_one_worker_report_row(run_dir):
    structured = {
        "verdict": "no-finding",
        "anchor_file": "/workspace/api/permissions.py",
        "task_id": 15,
        "summary": "Investigated permissions; no real-boundary sinks.",
        "findings": [],
    }
    _ingest_structured(run_dir, structured)

    with connect_db(run_dir) as conn:
        rows = list(conn.execute("SELECT verdict, title FROM worker_reports WHERE task_id = 15"))
        findings_rows = list(conn.execute("SELECT * FROM findings WHERE task_id = 15"))
    assert len(rows) == 1
    assert rows[0]["verdict"] == "no-finding"
    assert len(findings_rows) == 0


def test_one_finding_inserts_one_worker_report_and_one_findings_row(run_dir):
    structured = {
        "verdict": "finding",
        "anchor_file": "/workspace/api/views.py",
        "task_id": 18,
        "summary": "One ViewSet missing permission_classes.",
        "findings": [
            {
                "finding_type": "missing authorization",
                "cwe": "CWE-287",
                "severity": "high",
                "title": "EventViewSet missing auth",
                "sink_location": "api/views.py:295",
                "sink_category": "authorization",
                "summary": "EventViewSet has no permission_classes.",
                "source_to_sink": "router → EventViewSet",
                "why_insufficient": "no project-wide default permission classes either",
            }
        ],
    }
    _ingest_structured(run_dir, structured)

    with connect_db(run_dir) as conn:
        wr = list(conn.execute("SELECT verdict FROM worker_reports WHERE task_id = 18"))
        f = list(conn.execute("SELECT title, cwe, severity, anchor_file FROM findings WHERE task_id = 18"))
    assert len(wr) == 1
    assert wr[0]["verdict"] == "finding"
    assert len(f) == 1
    assert f[0]["cwe"] == "CWE-287"
    assert f[0]["severity"] == "high"
    assert f[0]["anchor_file"] == "/workspace/api/views.py"


def test_multiple_findings_insert_one_row_each(run_dir):
    structured = {
        "verdict": "finding",
        "anchor_file": "/workspace/api/views.py",
        "task_id": 18,
        "summary": "7 ViewSets missing permission_classes.",
        "findings": [
            {
                "finding_type": "missing authorization",
                "cwe": "CWE-287",
                "severity": "high",
                "title": f"ViewSet{i} missing auth",
                "sink_location": f"api/views.py:{100 + i*10}",
                "sink_category": "authorization",
                "why_insufficient": "no gate",
            }
            for i in range(7)
        ],
    }
    _ingest_structured(run_dir, structured)

    with connect_db(run_dir) as conn:
        wr = list(conn.execute("SELECT COUNT(*) AS n FROM worker_reports WHERE task_id = 18"))
        f = list(conn.execute("SELECT COUNT(*) AS n FROM findings WHERE task_id = 18"))
    assert wr[0]["n"] == 1
    assert f[0]["n"] == 7


def test_reingest_is_idempotent_within_task(run_dir):
    """Calling _ingest_structured twice with the same object should not
    duplicate findings rows."""
    structured = {
        "verdict": "finding",
        "anchor_file": "/workspace/test.py",
        "task_id": 1,
        "summary": "test",
        "findings": [
            {
                "finding_type": "missing auth",
                "cwe": "CWE-287",
                "severity": "high",
                "title": "x missing auth",
                "sink_location": "test.py:10",
                "sink_category": "authorization",
                "why_insufficient": "no gate",
            }
        ],
    }
    _ingest_structured(run_dir, structured)
    _ingest_structured(run_dir, structured)

    with connect_db(run_dir) as conn:
        f = list(conn.execute("SELECT COUNT(*) AS n FROM findings WHERE task_id = 1"))
    assert f[0]["n"] == 1


def test_render_no_finding_produces_frontmatter_and_summary():
    from report_utils import render_markdown_from_structured  # noqa: E402
    structured = {
        "verdict": "no-finding",
        "anchor_file": "/workspace/api/permissions.py",
        "task_id": 15,
        "summary": "Investigated permissions; no real-boundary sinks found.",
        "findings": [],
    }
    md = render_markdown_from_structured(structured)
    assert md.startswith("---\n")
    assert 'verdict: "no-finding"' in md
    assert 'task_id: 15' in md
    assert 'anchor_file: "/workspace/api/permissions.py"' in md
    assert "Investigated permissions" in md


def test_render_finding_includes_per_finding_blocks():
    from report_utils import render_markdown_from_structured  # noqa: E402
    structured = {
        "verdict": "finding",
        "anchor_file": "/workspace/api/views.py",
        "task_id": 18,
        "summary": "Two ViewSets missing permission_classes.",
        "findings": [
            {
                "finding_type": "missing authorization",
                "cwe": "CWE-287",
                "severity": "high",
                "title": "EventViewSet missing auth",
                "sink_location": "api/views.py:295",
                "sink_category": "authorization",
                "summary": "No permission_classes on EventViewSet.",
                "why_insufficient": "No project-wide default either.",
            },
            {
                "finding_type": "missing authorization",
                "cwe": "CWE-287",
                "severity": "medium",
                "title": "ChannelViewSet missing auth",
                "sink_location": "api/views.py:203",
                "sink_category": "authorization",
                "summary": "No permission_classes on ChannelViewSet.",
                "why_insufficient": "No project-wide default either.",
            },
        ],
    }
    md = render_markdown_from_structured(structured)
    # frontmatter has the top-level shape
    assert 'verdict: "finding"' in md
    assert 'task_id: 18' in md
    # both findings show up with their key facts
    assert "EventViewSet missing auth" in md
    assert "ChannelViewSet missing auth" in md
    assert "api/views.py:295" in md
    assert "api/views.py:203" in md
    assert "CWE-287" in md


def test_render_frontmatter_is_well_formed_yaml():
    import re
    import yaml
    from report_utils import render_markdown_from_structured  # noqa: E402
    structured = {
        "verdict": "finding",
        "anchor_file": "/workspace/x.py",
        "task_id": 1,
        "summary": "test",
        "findings": [
            {
                "finding_type": "missing auth",
                "cwe": "CWE-287",
                "severity": "high",
                "title": "test",
                "sink_location": "x.py:1",
                "sink_category": "authorization",
                "why_insufficient": "test",
            }
        ],
    }
    md = render_markdown_from_structured(structured)
    m = re.match(r"^---\n(.*?)\n---\n", md, re.DOTALL)
    assert m is not None, f"frontmatter not found in: {md[:200]!r}"
    fm = yaml.safe_load(m.group(1))
    assert fm["verdict"] == "finding"
    assert fm["task_id"] == 1
    assert fm["anchor_file"] == "/workspace/x.py"
