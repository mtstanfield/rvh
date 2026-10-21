"""The tasks table gains nullable region columns; null = whole-file (current
behavior). conftest.py puts scripts/ on sys.path."""
from __future__ import annotations

import common


def test_tasks_table_has_region_columns(tmp_path):
    with common.connect_db(tmp_path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "anchor_start_line" in cols
    assert "anchor_end_line" in cols
