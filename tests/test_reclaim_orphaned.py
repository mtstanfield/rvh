"""Resume recovery: tasks left `reserved`/`in_progress` by an unclean prior exit
(double Ctrl+C, SIGKILL, crash) are re-pended clean at engine startup, so
pick_next_batch re-dispatches them instead of leaving them stuck — and any
salvage sidecar is cleared so the re-run is fresh."""
from __future__ import annotations

import common
import run_autonomous_v2 as eng


def _add_file(conn, path):
    now = common.utc_now()
    cur = conn.execute(
        "INSERT INTO files (path, rel_dir, extension, size_bytes, is_binary, "
        "category, tags_json, created_at, updated_at) VALUES (?,?,?,?,0,?,?,?,?)",
        (path, "x", ".go", 4000, "first_party", "[]", now, now),
    )
    return cur.lastrowid


def _add_task(conn, fid, status, *, attempt_count=0, owner=None, session=None, leased=False):
    now = common.utc_now()
    lease = common.add_seconds(now, 1800) if leased else None
    cur = conn.execute(
        "INSERT INTO tasks (file_id, priority, status, owner, owner_session, "
        "attempt_count, reserved_at, lease_expires_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (fid, 5, status, owner, session, attempt_count,
         now if leased else None, lease, now, now),
    )
    return cur.lastrowid


def test_reclaim_repends_orphans_clears_salvage_leaves_others(tmp_path):
    run_dir = tmp_path / ".rvh-runs"
    run_dir.mkdir()
    with common.connect_db(run_dir) as conn:
        ip = _add_task(conn, _add_file(conn, "a.go"), common.TASK_IN_PROGRESS,
                       attempt_count=2, owner="auto", session="s1", leased=True)
        rsv = _add_task(conn, _add_file(conn, "b.go"), common.TASK_RESERVED,
                        attempt_count=1, owner="auto", session="s1", leased=True)
        pend = _add_task(conn, _add_file(conn, "c.go"), common.TASK_PENDING)
        comp = _add_task(conn, _add_file(conn, "d.go"), common.TASK_COMPLETED)

    # the orphaned in-flight task left a salvage sidecar behind
    salv = eng._salvage_sidecar_path(run_dir, ip)
    salv.parent.mkdir(parents=True, exist_ok=True)
    salv.write_text("partial interrupted analysis")

    n = eng.reclaim_orphaned_tasks(run_dir)
    assert n == 2  # the in_progress + reserved orphans

    with common.connect_db(run_dir) as conn:
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, status, owner, lease_expires_at, attempt_count FROM tasks"
        )}
    # orphans → requeued, lease cleared, attempt decremented (no penalty)
    assert rows[ip]["status"] == common.TASK_REQUEUED
    assert rows[ip]["owner"] is None and rows[ip]["lease_expires_at"] is None
    assert rows[ip]["attempt_count"] == 1
    assert rows[rsv]["status"] == common.TASK_REQUEUED
    assert rows[rsv]["attempt_count"] == 0
    # non-orphans untouched
    assert rows[pend]["status"] == common.TASK_PENDING
    assert rows[comp]["status"] == common.TASK_COMPLETED
    # salvage cleared → resume does a full fresh re-run, not salvage-write
    assert not salv.exists()


def test_reclaim_noop_when_no_orphans(tmp_path):
    run_dir = tmp_path / ".rvh-runs"
    run_dir.mkdir()
    with common.connect_db(run_dir) as conn:
        _add_task(conn, _add_file(conn, "a.go"), common.TASK_PENDING)
    assert eng.reclaim_orphaned_tasks(run_dir) == 0
