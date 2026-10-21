"""Tests for bootstrap's `stop_after` early-exit (used by run_repo_vuln_hunt
to overlap CPG build with downstream bootstrap phases)."""
from unittest.mock import patch

import pytest

import bootstrap


def test_stop_after_rejects_unknown_phase(tmp_path):
    with pytest.raises(ValueError):
        bootstrap.bootstrap(
            tmp_path, tmp_path / "run",
            stop_after="not-a-real-phase",
            endpoint="http://e", model="m",
        )


def test_stop_after_inventoried_short_circuits_when_status_already_past(tmp_path):
    """If current status is already >= stop_after, bootstrap returns
    immediately after the natural 'no-op' skip — no phase scripts run."""
    run_dir = tmp_path / "run"
    with patch("bootstrap.current_run_status", return_value="scored"), \
         patch("bootstrap.run_step") as rs:
        bootstrap.bootstrap(
            tmp_path, run_dir,
            stop_after="inventoried",
            endpoint="http://e", model="m",
        )
    # No phase script was invoked — status was already past stop_after
    assert rs.call_count == 0


def test_stop_after_inventoried_runs_init_and_inventory_only(tmp_path):
    """Starting from nothing, stop_after=inventoried must run init_run +
    inventory_repo but NOT analyze/score."""
    run_dir = tmp_path / "run"
    # Fake status progression: None → after init step → "initialized" → after
    # inventory step → "inventoried" (we check _should_stop at each boundary).
    with patch("bootstrap.current_run_status", return_value=None), \
         patch("bootstrap.run_step") as rs:
        bootstrap.bootstrap(
            tmp_path, run_dir,
            stop_after="inventoried",
            endpoint="http://e", model="m",
        )
    # Exactly two steps ran: init_run + inventory_repo
    labels = [call.args[0] for call in rs.call_args_list]
    assert labels == ["init_run", "inventory_repo"], (
        f"expected init+inventory only; ran {labels}"
    )


def test_stop_after_analyzed_includes_analyze_step(tmp_path):
    run_dir = tmp_path / "run"
    with patch("bootstrap.current_run_status", return_value=None), \
         patch("bootstrap.run_step") as rs:
        bootstrap.bootstrap(
            tmp_path, run_dir,
            stop_after="analyzed",
            endpoint="http://e", model="m",
        )
    labels = [call.args[0] for call in rs.call_args_list]
    # derive_repo_profile is chained into the analyze logical phase (no new
    # status); it runs whenever analyze_repo runs.
    assert labels == [
        "init_run", "inventory_repo", "analyze_repo", "derive_repo_profile",
    ], f"expected init+inventory+analyze+derive; ran {labels}"


def test_no_stop_after_runs_all_phases_from_fresh(tmp_path):
    """Default behavior: no stop_after → all phases run."""
    run_dir = tmp_path / "run"
    with patch("bootstrap.current_run_status", return_value=None), \
         patch("bootstrap.run_step") as rs:
        bootstrap.bootstrap(
            tmp_path, run_dir,
            endpoint="http://e", model="m",
        )
    labels = [call.args[0] for call in rs.call_args_list]
    assert labels == [
        "init_run", "inventory_repo", "analyze_repo", "derive_repo_profile",
        "score_files",
    ], f"expected full sequence; ran {labels}"


def test_stop_after_then_resume_completes_remaining_phases(tmp_path):
    """Two-slice invocation: first call with stop_after=inventoried runs
    init+inventory; second call (no stop_after) resumes from 'inventoried'
    and runs the remaining phases. This is the pattern
    run_repo_vuln_hunt.py uses to overlap CPG build with analyze/score."""
    run_dir = tmp_path / "run"

    # First slice: status is None, progresses to inventoried
    with patch("bootstrap.current_run_status", return_value=None), \
         patch("bootstrap.run_step") as rs1:
        bootstrap.bootstrap(
            tmp_path, run_dir,
            stop_after="inventoried",
            endpoint="http://e", model="m",
        )
    assert [c.args[0] for c in rs1.call_args_list] == [
        "init_run", "inventory_repo",
    ]

    # Second slice: simulate DB status now "inventoried" (as if the inventory
    # phase persisted its result). Should resume and run analyze/score/build.
    with patch("bootstrap.current_run_status", return_value="inventoried"), \
         patch("bootstrap.run_step") as rs2:
        bootstrap.bootstrap(
            tmp_path, run_dir,
            endpoint="http://e", model="m",
        )
    assert [c.args[0] for c in rs2.call_args_list] == [
        "analyze_repo", "derive_repo_profile", "score_files",
    ]
