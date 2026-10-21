"""Manual stop (Ctrl+C / stop-file) requeues the in-flight task for a CLEAN
re-run on resume — no salvage stash, no attempt penalty — so resume never enters
salvage-write mode on the interrupted task."""
from __future__ import annotations

from types import SimpleNamespace

import common
import run_autonomous_v2 as eng


def _seed_in_progress_task(run_dir, *, owner, session, attempt_count=1):
    with common.connect_db(run_dir) as conn:
        now = common.utc_now()
        cur = conn.execute(
            "INSERT INTO files (path, rel_dir, extension, size_bytes, is_binary, "
            "category, tags_json, created_at, updated_at) VALUES (?,?,?,?,0,?,?,?,?)",
            ("svc/handler.go", "svc", ".go", 4000, "first_party", "[]", now, now),
        )
        fid = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO tasks (file_id, priority, status, owner, owner_session, "
            "attempt_count, reserved_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (fid, 5, common.TASK_IN_PROGRESS, owner, session, attempt_count, now, now, now),
        )
        return cur.lastrowid


def _state(task_id, session):
    return {
        "task_id": task_id,
        "reservation": {"task_id": task_id, "path": "svc/handler.go", "priority": 5},
        "dispatch_id": session,
        "handle": {"dry_run": True},  # terminate_worker is a no-op on dry_run
    }


def test_interrupt_requeues_clean_and_clears_salvage(tmp_path):
    run_dir = tmp_path / ".rvh-runs"
    run_dir.mkdir()
    owner, session = "auto", "auto-0001-deadbeef"
    task_id = _seed_in_progress_task(run_dir, owner=owner, session=session, attempt_count=1)

    # A prior partial attempt left a salvage sidecar; resume would
    # otherwise read these and enter salvage-write mode instead of re-running.
    salv = eng._salvage_sidecar_path(run_dir, task_id)
    salv.parent.mkdir(parents=True, exist_ok=True)
    salv.write_text("partial interrupted analysis")

    args = SimpleNamespace(owner=owner)
    record = eng.requeue_interrupted_task(
        args, run_dir, _state(task_id, session), "interrupted by manual stop"
    )

    assert record["final_status"] == common.TASK_REQUEUED
    with common.connect_db(run_dir) as conn:
        row = conn.execute(
            "SELECT status, owner, reserved_at, attempt_count FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    assert row["status"] == common.TASK_REQUEUED   # re-dispatchable on resume
    assert row["owner"] is None                     # lease cleared
    assert row["reserved_at"] is None
    assert row["attempt_count"] == 0                # interrupt costs no attempt
    assert not salv.exists()                        # no salvage → full fresh re-run


def test_interrupt_requeue_clean_when_no_sidecars(tmp_path):
    run_dir = tmp_path / ".rvh-runs"
    run_dir.mkdir()
    owner, session = "auto", "auto-0002-cafef00d"
    task_id = _seed_in_progress_task(run_dir, owner=owner, session=session, attempt_count=1)

    args = SimpleNamespace(owner=owner)
    eng.requeue_interrupted_task(
        args, run_dir, _state(task_id, session), "interrupted"
    )

    with common.connect_db(run_dir) as conn:
        row = conn.execute(
            "SELECT status, attempt_count FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert row["status"] == common.TASK_REQUEUED
    assert row["attempt_count"] == 0
