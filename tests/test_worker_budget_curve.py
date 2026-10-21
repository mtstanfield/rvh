"""run_autonomous_v2.worker_wall_budget_seconds — the per-priority worker wall-clock cap."""
from run_autonomous_v2 import worker_wall_budget_seconds


def test_priority_5_gets_an_hour():
    assert worker_wall_budget_seconds(5) == 3600


def test_lower_priorities_get_thirty_minutes():
    assert [worker_wall_budget_seconds(p) for p in (4, 3, 2, 1)] == [1800] * 4


def test_out_of_range_priorities_follow_the_nearest_tier():
    assert worker_wall_budget_seconds(9) == 3600
    assert worker_wall_budget_seconds(0) == 1800
