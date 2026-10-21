"""Integration test for the budget-kill salvage path in collect_task.

The 2026-05-06 coriolix run produced 6 budget kills that all dropped any
analysis the model had reached pre-SIGKILL. This test pins the new
behavior: when state.budget_killed is True and the task hasn't hit the
attempt cap, collect_task stashes the worker's last assistant turn into
.salvage/task-N.txt and requeues (TASK_REQUEUED) so the next attempt
runs the salvage_write recipe instead of starting from scratch.
"""
from __future__ import annotations

import argparse
import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from common import (
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_REQUEUED,
    connect_db,
    ensure_run_dirs,
    init_db,
    utc_now,
)
import run_autonomous_v2


_GOOSE_BANNER = (
    "    __( O)>  ● new session\n"
    "   \\____)    20260506_1 · /workspace\n"
    "     L L     goose is ready\n"
)


def _seed_run(tmp_path: Path, attempt_count: int = 1) -> tuple[Path, int]:
    """Create the minimal DB layout collect_task needs: a runs row, one
    files row, one tasks row in TASK_IN_PROGRESS bound to owner ``test``."""
    run_dir = tmp_path / ".rvh-runs"
    init_db(run_dir)
    now = utc_now()
    with connect_db(run_dir) as conn:
        conn.execute(
            "INSERT INTO runs (id, label, repo_root, created_at, updated_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("test-run", "test", str(tmp_path), now, now, "running"),
        )
        conn.execute(
            "INSERT INTO files (path, rel_dir, extension, size_bytes, is_binary, "
            "category, tags_json, excluded, created_at, updated_at) "
            "VALUES (?, '', 'py', 100, 0, 'code', '[]', 0, ?, ?)",
            ("api/views.py", now, now),
        )
        file_id = conn.execute(
            "SELECT id FROM files WHERE path = 'api/views.py'"
        ).fetchone()["id"]
        cursor = conn.execute(
            "INSERT INTO tasks (file_id, priority, status, owner, owner_session, "
            "attempt_count, created_at, updated_at) "
            "VALUES (?, 3, ?, 'test', 'sess-1', ?, ?, ?)",
            (file_id, TASK_IN_PROGRESS, attempt_count, now, now),
        )
        task_id = cursor.lastrowid
    return run_dir, task_id


def _make_state(
    run_dir: Path,
    task_id: int,
    *,
    budget_killed: bool,
    stdout_text: str,
    returncode: int = -9,
) -> dict:
    """Build the in_flight state dict collect_task expects."""
    work_log_dir = run_dir / "tasks" / "auto-0001-test-task-1"
    work_log_dir.mkdir(parents=True)
    stdout_path = work_log_dir / "stdout.txt"
    stderr_path = work_log_dir / "stderr.txt"
    last_message_path = work_log_dir / "last_message.txt"
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    last_message_path.write_text("", encoding="utf-8")

    proc = MagicMock()
    proc.returncode = returncode
    proc.poll.return_value = returncode  # already exited
    proc.wait.return_value = None

    handle = {
        "process": proc,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_file": MagicMock(closed=True),
        "stderr_file": MagicMock(closed=True),
        "last_message_path": last_message_path,
        "launched_at": 1.0,
        "dry_run": False,
        "mode": "subprocess",
        "container_name": None,
    }

    state = {
        "task_id": task_id,
        "reservation": {
            "task_id": task_id,
            "path": "api/views.py",
            "priority": 3,
        },
        "dispatch_payload": {
            "task_id": task_id,
            "anchor_file": "api/views.py",
            "report_path": str(run_dir / "reports" / f"task-{task_id}.md"),
            "host_report_path": str(run_dir / "reports" / f"task-{task_id}.md"),
        },
        "dispatch_id": "sess-1",
        "handle": handle,
    }
    if budget_killed:
        state["budget_killed"] = True
        state["budget_seconds"] = 480
    return state


def _goose_stdout(text: str) -> str:
    return _GOOSE_BANNER + json.dumps({
        "messages": [
            {"role": "assistant",
             "content": [{"type": "text", "text": text}]},
        ]
    })


def _make_args() -> argparse.Namespace:
    return argparse.Namespace(
        owner="test",
        rate_limit_cooldown_seconds=300,
        debug=False,
        dry_run=False,
    )


def test_budget_kill_stashes_salvage_and_requeues(tmp_path):
    run_dir, task_id = _seed_run(tmp_path, attempt_count=1)
    prior = "Found yaml.FullLoader on external HTTP response — this is a finding."
    state = _make_state(
        run_dir, task_id,
        budget_killed=True,
        stdout_text=_goose_stdout(prior),
    )

    record = run_autonomous_v2.collect_task(
        _make_args(), run_dir, state, max_attempts=3,
    )

    # Salvage sidecar was created with the extracted analysis.
    sidecar = run_dir / "reports" / ".salvage" / f"task-{task_id}.txt"
    assert sidecar.exists()
    assert prior in sidecar.read_text(encoding="utf-8")

    # Task is requeued for a salvage attempt rather than terminally failed.
    assert record["final_status"] == TASK_REQUEUED
    with connect_db(run_dir) as conn:
        row = conn.execute(
            "SELECT status, last_error, notes FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    assert row["status"] == TASK_REQUEUED
    assert "budget exhausted" in row["last_error"]
    assert "requeued" in (row["notes"] or "")


def test_budget_kill_with_no_recoverable_text_still_requeues(tmp_path):
    """SIGKILL before the model produced any assistant text → no salvage,
    but still requeue (next attempt re-runs full investigation)."""
    run_dir, task_id = _seed_run(tmp_path, attempt_count=1)
    state = _make_state(
        run_dir, task_id,
        budget_killed=True,
        stdout_text=_GOOSE_BANNER,  # banner only, no JSON
    )

    record = run_autonomous_v2.collect_task(
        _make_args(), run_dir, state, max_attempts=3,
    )

    assert not (run_dir / "reports" / ".salvage" / f"task-{task_id}.txt").exists()
    assert record["final_status"] == TASK_REQUEUED


def test_budget_kill_at_max_attempts_fails_terminally(tmp_path):
    """When attempt_count is already at the cap, requeueing would just
    loop. Fall through to TASK_FAILED and clear any prior salvage sidecar."""
    run_dir, task_id = _seed_run(tmp_path, attempt_count=3)
    # Pre-stash a salvage sidecar from a prior attempt — terminal-fail
    # path should clean it up so it doesn't bleed into a future re-priority.
    sidecar = run_dir / "reports" / ".salvage" / f"task-{task_id}.txt"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("stale", encoding="utf-8")

    state = _make_state(
        run_dir, task_id,
        budget_killed=True,
        stdout_text=_goose_stdout("would have salvaged but at limit"),
    )

    record = run_autonomous_v2.collect_task(
        _make_args(), run_dir, state, max_attempts=3,
    )

    assert record["final_status"] == TASK_FAILED
    assert not sidecar.exists(), "stale salvage sidecar should be cleared"
    with connect_db(run_dir) as conn:
        row = conn.execute(
            "SELECT status, notes FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert row["status"] == TASK_FAILED
    assert "max attempts reached" in row["notes"]


# ---------------------------------------------------------------------------
# Loop guard: don't re-stash when a recovery attempt itself fails
# ---------------------------------------------------------------------------

def test_budget_kill_of_salvage_attempt_skips_restash(tmp_path):
    """A salvage attempt's stdout is mid-format output, not a fresh
    analysis. If THAT gets budget-killed, stashing it as a new salvage
    sidecar would drive the next attempt into salvage_write against
    garbage. The recovery_mode flag prevents that — next attempt falls
    back to a full re-run."""
    run_dir, task_id = _seed_run(tmp_path, attempt_count=2)
    state = _make_state(
        run_dir, task_id,
        budget_killed=True,
        stdout_text=_goose_stdout("(half-formatted finding markdown...)"),
    )
    state["recovery_mode"] = "salvage"

    record = run_autonomous_v2.collect_task(
        _make_args(), run_dir, state, max_attempts=3,
    )

    sidecar = run_dir / "reports" / ".salvage" / f"task-{task_id}.txt"
    assert not sidecar.exists(), \
        "should NOT stash from a recovery attempt's broken stdout"
    assert record["final_status"] == TASK_REQUEUED


def test_expected_report_missing_on_salvage_attempt_skips_restash(tmp_path):
    """If a salvage attempt exits 0 but didn't write the parent, the
    pre-raise stash logic must NOT replace the original salvage sidecar
    with the broken salvage stdout — same hazard as the budget-kill case."""
    run_dir, task_id = _seed_run(tmp_path, attempt_count=2)
    state = _make_state(
        run_dir, task_id,
        budget_killed=False,
        stdout_text=_goose_stdout("Garbage from broken salvage attempt"),
        returncode=0,  # clean exit, but no parent
    )
    state["recovery_mode"] = "salvage"

    record = run_autonomous_v2.collect_task(
        _make_args(), run_dir, state, max_attempts=3,
    )

    sidecar = run_dir / "reports" / ".salvage" / f"task-{task_id}.txt"
    assert not sidecar.exists(), \
        "should NOT overwrite original salvage sidecar with broken retry stdout"
    assert record["final_status"] == TASK_REQUEUED
