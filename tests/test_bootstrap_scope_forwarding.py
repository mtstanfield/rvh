"""Regression: bootstrap must forward repo-scoping flags
(--include-path/--exclude-path/--exclude-glob) into the init_run command.

Previously these were parsed by run_repo_vuln_hunt.py and supported by
init_run.py, but bootstrap() in the middle neither accepted nor forwarded them,
so `rvh --include-path app` silently inventoried the WHOLE repo (config
include_paths stayed []). This pins the wiring so the scope can't evaporate."""
from unittest.mock import patch

import bootstrap


def _init_cmd(rs):
    return next(c.args[1] for c in rs.call_args_list if c.args[0] == "init_run")


def test_init_run_forwards_scope_flags(tmp_path):
    run_dir = tmp_path / "run"
    with patch("bootstrap.current_run_status", return_value=None), \
         patch("bootstrap.run_step") as rs:
        bootstrap.bootstrap(
            tmp_path, run_dir,
            stop_after="inventoried",
            endpoint="http://e", model="m",
            include_paths=["app"],
            exclude_paths=["app/Test"],
            exclude_globs=["*/Vendor/*"],
        )
    cmd = _init_cmd(rs)
    # each scope flag is forwarded with its value immediately after it
    assert cmd[cmd.index("--include-path") + 1] == "app"
    assert cmd[cmd.index("--exclude-path") + 1] == "app/Test"
    assert cmd[cmd.index("--exclude-glob") + 1] == "*/Vendor/*"


def test_multiple_include_paths_all_forwarded(tmp_path):
    run_dir = tmp_path / "run"
    with patch("bootstrap.current_run_status", return_value=None), \
         patch("bootstrap.run_step") as rs:
        bootstrap.bootstrap(
            tmp_path, run_dir,
            stop_after="inventoried",
            endpoint="http://e", model="m",
            include_paths=["app/Controller", "app/Model", "app/Lib"],
        )
    cmd = _init_cmd(rs)
    pairs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--include-path"]
    assert pairs == ["app/Controller", "app/Model", "app/Lib"]


def test_init_run_omits_scope_flags_when_unscoped(tmp_path):
    run_dir = tmp_path / "run"
    with patch("bootstrap.current_run_status", return_value=None), \
         patch("bootstrap.run_step") as rs:
        bootstrap.bootstrap(tmp_path, run_dir, stop_after="inventoried", endpoint="http://e", model="m")
    cmd = _init_cmd(rs)
    assert "--include-path" not in cmd
    assert "--exclude-path" not in cmd
    assert "--exclude-glob" not in cmd


