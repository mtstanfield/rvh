"""``rvh`` must rebuild a broken .venv instead of dying on it.

A venv copied between machines (or left behind by an uninstalled Python) has a
dangling interpreter symlink. ``rvh`` detects that correctly — ``[ ! -x
.venv/bin/python ]`` is true for a dangling link — but ``python3 -m venv`` will
not repair an existing venv in place; it fails with ``[Errno 2] No such file or
directory`` and ``set -e`` kills the script. The venv has to be removed first.

These tests drive the real ``rvh`` file with a stub ``python3`` on PATH, so
they exercise its actual control flow without creating a venv or hitting the
network. The stub reproduces CPython's refusal to overwrite a dangling
interpreter, which is the behavior that made this a hard failure.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

RVH = Path(__file__).resolve().parent.parent / "rvh"

FAKE_PYTHON3 = """#!/usr/bin/env bash
# Stub python3. Emulates `python3 -m venv DIR`, including the real venv
# module's refusal to overwrite an existing dangling interpreter symlink.
if [ "$1" = "-c" ]; then exec {real_python} "$@"; fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    d="$3"
    if [ -L "$d/bin/python" ] && [ ! -e "$d/bin/python" ]; then
        echo "Error: [Errno 2] No such file or directory: '$d/bin/python'" >&2
        exit 1
    fi
    mkdir -p "$d/bin"
    printf '#!/bin/sh\\necho FAKE_VENV_PYTHON_RAN\\n' > "$d/bin/python"
    printf '#!/bin/sh\\nexit 0\\n' > "$d/bin/pip"
    chmod +x "$d/bin/python" "$d/bin/pip"
    exit 0
fi
exit 0
"""


def _sandbox(tmp_path: Path) -> tuple[Path, dict]:
    """A tmp dir holding the real rvh, a stub entrypoint, and a stub python3."""
    shutil.copy2(RVH, tmp_path / "rvh")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "run_repo_vuln_hunt.py").write_text("print('entrypoint reached')\n")

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    py = bin_dir / "python3"
    py.write_text(FAKE_PYTHON3.format(real_python=sys.executable))
    py.chmod(py.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return tmp_path, env


def _break_venv(root: Path) -> None:
    """Leave behind the exact artifact a cross-machine copy produces."""
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to("/nonexistent/homebrew/bin/python3.14")


def _run(root: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(root / "rvh"), "--help"],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=60,
    )


def test_rvh_rebuilds_a_broken_venv(tmp_path):
    """A dangling interpreter symlink must be rebuilt, not fatal."""
    root, env = _sandbox(tmp_path)
    _break_venv(root)

    proc = _run(root, env)

    assert proc.returncode == 0, (
        f"rvh died on a broken .venv (exit {proc.returncode}).\n"
        f"stderr:\n{proc.stderr}"
    )
    assert "FAKE_VENV_PYTHON_RAN" in proc.stdout, (
        f"rvh did not exec the rebuilt venv interpreter; stdout={proc.stdout!r} "
        f"stderr={proc.stderr!r}"
    )
    assert (root / ".venv" / "bin" / "python").exists(), (
        ".venv/bin/python is still dangling after rvh ran"
    )


def test_rvh_creates_venv_on_first_run(tmp_path):
    """The no-.venv path must keep working — the repair must not break it."""
    root, env = _sandbox(tmp_path)

    proc = _run(root, env)

    assert proc.returncode == 0, f"exit {proc.returncode}; stderr:\n{proc.stderr}"
    assert "FAKE_VENV_PYTHON_RAN" in proc.stdout
    assert (root / ".venv" / "bin" / "python").exists()


def test_rvh_reuses_a_healthy_venv(tmp_path):
    """A working venv must be left alone — no gratuitous rebuild on every run."""
    root, env = _sandbox(tmp_path)
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    marker = bin_dir / "python"
    marker.write_text("#!/bin/sh\necho HEALTHY_VENV_PYTHON\n")
    marker.chmod(marker.stat().st_mode | stat.S_IEXEC)
    (bin_dir / "canary").write_text("installed packages live here")

    proc = _run(root, env)

    assert proc.returncode == 0, f"exit {proc.returncode}; stderr:\n{proc.stderr}"
    assert "HEALTHY_VENV_PYTHON" in proc.stdout, (
        f"existing venv interpreter was not used; stdout={proc.stdout!r}"
    )
    assert (bin_dir / "canary").exists(), (
        "rvh destroyed a healthy .venv — installed packages would be lost"
    )


def test_rvh_eval_runs_the_eval_cli_and_everything_else_runs_the_scan(tmp_path):
    """``rvh eval …`` goes to scripts/eval_cli.py; any other first argument to the scan."""
    root, env = _sandbox(tmp_path)
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text('#!/bin/sh\necho "ARGS: $*"\n')
    python.chmod(python.stat().st_mode | stat.S_IEXEC)

    def run(*argv):
        return subprocess.run([str(root / "rvh"), *argv], cwd=str(root), env=env,
                              capture_output=True, text=True, timeout=60).stdout.strip()

    assert run("eval", "compare", "a", "b") == "ARGS: scripts/eval_cli.py compare a b"
    assert run("--repo", "x") == "ARGS: scripts/run_repo_vuln_hunt.py --repo x"
