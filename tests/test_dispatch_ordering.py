"""Decomposed slice sub-tasks must dispatch where their parent file task would
have — not last in their priority tier just because they got new high ids.
conftest.py puts scripts/ on sys.path."""
from __future__ import annotations

import common
from run_autonomous_v2 import pick_next_batch


def _mkfile(conn, path, now):
    return conn.execute(
        "INSERT INTO files (path, rel_dir, extension, size_bytes, is_binary, "
        "category, tags_json, created_at, updated_at) VALUES (?,?,?,?,0,?,?,?,?)",
        (path, "x", ".py", 100, "first_party", "[]", now, now),
    ).lastrowid


def test_slices_dispatch_at_parent_position_not_last(tmp_path):
    with common.connect_db(tmp_path) as conn:
        now = common.utc_now()
        f_big = _mkfile(conn, "big.py", now)   # the decomposed file
        f_a = _mkfile(conn, "a.py", now)       # a later whole-file task

        # task 1: parent of the slices, now superseded (not dispatchable)
        parent = conn.execute(
            "INSERT INTO tasks (file_id,priority,status,created_at,updated_at) "
            "VALUES (?,5,'decomposed',?,?)", (f_big, now, now)
        ).lastrowid
        # task 2: a whole-file priority-5 task with a LOWER id than the slices
        whole = conn.execute(
            "INSERT INTO tasks (file_id,priority,status,created_at,updated_at) "
            "VALUES (?,5,'pending',?,?)", (f_a, now, now)
        ).lastrowid
        # tasks 3,4: slices of big.py, priority 5, high ids, parent_task_id=parent
        s1 = conn.execute(
            "INSERT INTO tasks (file_id,priority,status,anchor_start_line,"
            "anchor_end_line,parent_task_id,created_at,updated_at) "
            "VALUES (?,5,'pending',1,500,?,?,?)", (f_big, parent, now, now)
        ).lastrowid
        s2 = conn.execute(
            "INSERT INTO tasks (file_id,priority,status,anchor_start_line,"
            "anchor_end_line,parent_task_id,created_at,updated_at) "
            "VALUES (?,5,'pending',501,1000,?,?,?)", (f_big, parent, now, now)
        ).lastrowid

        rows = pick_next_batch(conn, 1, 10)
        order = [r["task_id"] for r in rows]

    # slices (s1,s2) slot in at the parent's position (id < whole), so they come
    # BEFORE the higher-id whole-file task — not after it.
    assert order == [s1, s2, whole]
