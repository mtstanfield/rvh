"""The worker prompt gains a region scope block when the task carries a region;
null region → empty string (unchanged whole-file behavior)."""
from __future__ import annotations

import worker


def test_region_block_present_for_region():
    out = worker._build_region_block({"anchor_start_line": 4001, "anchor_end_line": 4662})
    assert "4001" in out and "4662" in out
    assert "sink line" in out.lower()


def test_region_block_empty_when_no_region():
    assert worker._build_region_block({}) == ""
    assert worker._build_region_block({"anchor_start_line": None, "anchor_end_line": None}) == ""
