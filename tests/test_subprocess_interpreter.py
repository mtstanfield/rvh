"""Every Python subprocess must run under the *current* interpreter.

Regression tests for the bare-``python3`` bug: the pipeline used to spawn its
child stages with the literal string ``"python3"``, resolved from ``PATH``.
That meant ``rvh``'s ``.venv`` covered only the top-level process — every stage
that did real work ran under whatever interpreter PATH happened to hand it.

It worked by accident on machines where the global interpreter had picked up
pyyaml et al. over time, and failed with ``ModuleNotFoundError: No module
named 'yaml'`` on a fresh box, in a way that ``pip install -e '.[dev]'`` into
the venv could not fix.

``sys.executable`` is the venv interpreter when the parent is running under
one, so children inherit the parent's environment.
"""
from __future__ import annotations

import sys
from pathlib import Path

import bootstrap as b
import run_repo_vuln_hunt as r


def test_worker_runtime_imports_are_base_dependencies():
    """Modules imported unconditionally on the worker launch path must be
    declared in ``[project] dependencies``, not in an optional extra.

    ``rvh`` bootstraps with ``pip install -e .`` (no extras), so anything the
    run path imports has to be a base dependency. pyyaml was declared under
    ``[dev]`` while ``worker.py`` imported it to render the per-task recipe —
    a fresh install crashed the moment the first worker launched.
    """
    import tomllib

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    cfg = tomllib.loads(pyproject.read_text())
    base = " ".join(cfg["project"]["dependencies"]).lower()

    for module, dist in (("yaml", "pyyaml"), ("requests", "requests")):
        assert dist in base, (
            f"{module!r} is imported on the run path but {dist!r} is not a base "
            f"dependency; base = {cfg['project']['dependencies']}"
        )


def test_bootstrap_steps_use_current_interpreter(tmp_path, monkeypatch):
    """Every bootstrap phase must spawn its stage with sys.executable.

    Walks the full state machine (init -> inventory -> analyze -> score)
    with run_step captured, so every spawn site is covered in one pass.
    """
    captured: list[list[str]] = []

    def _capture(label, cmd, env=None):
        captured.append(cmd)

    monkeypatch.setattr(b, "run_step", _capture)
    # No DB -> status None -> bootstrap walks every phase.
    monkeypatch.setattr(b, "current_run_status", lambda run_dir: None)

    b.bootstrap(
        repo=tmp_path / "repo",
        run_dir=tmp_path / "run",
        endpoint="http://e",
        model="test-model",
    )

    assert captured, "bootstrap ran no steps — test drove nothing"

    python_steps = [
        c for c in captured
        if c and (c[0] == sys.executable or Path(str(c[0])).name.startswith("python"))
    ]
    assert python_steps, f"no Python steps captured; got: {captured}"

    bad = [c for c in python_steps if c[0] != sys.executable]
    assert not bad, (
        f"{len(bad)} bootstrap step(s) spawned with a PATH-resolved interpreter "
        f"instead of sys.executable ({sys.executable!r}): "
        + "; ".join(f"{c[0]!r} -> {Path(c[1]).name}" for c in bad)
    )


def test_engine_command_uses_current_interpreter():
    """build_engine_command must launch run_autonomous_v2 with sys.executable.

    This is the exact site that crashed: the engine subprocess is where the
    worker launch path imports yaml.
    """
    args = r.build_parser().parse_args(
        ["--repo", "/tmp/testrepo", "--endpoint", "http://e", "--model", "m"]
    )
    cmd, _env = r.build_engine_command(args, Path("/tmp/run"), "rvh-worker:x", "rvh-x-net", False)
    assert cmd[0] == sys.executable, (
        f"engine spawned with {cmd[0]!r}, expected sys.executable "
        f"({sys.executable!r}); full cmd: {cmd}"
    )
