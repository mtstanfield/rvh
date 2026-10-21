"""Tests for the container reaper in worker.launch_worker_async.

The reaper exists because `docker run --rm --name X` fails with
"Conflict. The container name '/X' is already in use" when a previous
attempt left a stale container behind (process killed mid-run before
--rm cleanup ran). Observed on coriolix-3.0.1 task-105 retry 2026-04-27.
The reaper does `docker rm -f X` synchronously before each launch.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from worker import launch_worker_async


@pytest.fixture
def container_config():
    return {"image_tag": "rvh-test:latest"}


def _fake_subprocess_module(monkeypatch):
    """Replace worker.subprocess with a mock that captures all calls.

    Returns the mock module so tests can assert call shape. The mock's
    `run` returns CompletedProcess-like objects; `Popen` returns a
    fake handle whose .stdin/.poll/.wait look real enough for the
    launch path's needs.
    """
    fake_subprocess = MagicMock()
    fake_subprocess.DEVNULL = -3
    fake_subprocess.PIPE = -1
    fake_proc = MagicMock()
    fake_proc.stdin = MagicMock()
    fake_subprocess.Popen.return_value = fake_proc
    fake_subprocess.run.return_value = MagicMock(returncode=0)
    monkeypatch.setattr("worker.subprocess", fake_subprocess)
    return fake_subprocess


def test_reaper_runs_before_docker_run_when_container_named(
    tmp_path, container_config, monkeypatch
):
    """The reaper must execute `docker rm -f <name>` before the launcher
    issues `docker run --name <name>`. Order matters — if the reaper
    runs after, the container-name conflict still fires."""
    fake_sub = _fake_subprocess_module(monkeypatch)

    launch_worker_async(
        repo_root=tmp_path,
        prompt="hello",
        task_dir=tmp_path / "logs",
        image="rvh-test:latest",
        network="rvh-test-net",
        worker_endpoint="http://localhost:8001",
        worker_api_key="dummy",
        worker_model="m",
        container_name="rvh-test-w-foo-1",
    )

    # First subprocess.run call must be the docker reaper.
    assert fake_sub.run.called, "reaper must call subprocess.run"
    reap_args = fake_sub.run.call_args_list[0]
    assert reap_args.args[0] == [
        "docker", "rm", "-f", "rvh-test-w-foo-1"
    ], f"reaper command shape wrong: {reap_args.args[0]!r}"
    # check=False so a missing container doesn't crash the launcher.
    assert reap_args.kwargs.get("check") is False
    # Stdout/stderr suppressed so the reaper's "no such container"
    # noise doesn't pollute the worker log.
    assert reap_args.kwargs.get("stdout") == fake_sub.DEVNULL
    assert reap_args.kwargs.get("stderr") == fake_sub.DEVNULL

    # And docker run is invoked after.
    popen_call = fake_sub.Popen.call_args
    popen_argv = popen_call.args[0]
    assert popen_argv[:2] == ["docker", "run"], (
        f"expected docker run after reaper; got {popen_argv[:5]!r}"
    )
    assert "rvh-test-w-foo-1" in popen_argv


def test_reaper_skipped_when_container_name_unset(
    tmp_path, container_config, monkeypatch
):
    """The reaper only runs when the launch is using --name. Without a
    container name the reaper has no target and should be skipped (don't
    invoke `docker rm -f` with no arg)."""
    fake_sub = _fake_subprocess_module(monkeypatch)

    launch_worker_async(
        repo_root=tmp_path,
        prompt="hello",
        task_dir=tmp_path / "logs",
        image="rvh-test:latest",
        network="rvh-test-net",
        worker_endpoint="http://localhost:8001",
        worker_api_key="dummy",
        worker_model="m",
        container_name=None,
    )

    # No docker rm call should appear.
    rm_calls = [
        c for c in fake_sub.run.call_args_list
        if c.args and isinstance(c.args[0], list) and c.args[0][:3] == ["docker", "rm", "-f"]
    ]
    assert rm_calls == [], (
        f"reaper called without container_name: {rm_calls!r}"
    )


def test_reaper_failure_does_not_abort_launch(
    tmp_path, container_config, monkeypatch
):
    """If `docker rm -f` fails (e.g. no docker daemon, or some odd
    container-state error), the launcher should still attempt
    docker run — the failure mode for a stale container is the same
    as no container, so `docker run` will either succeed or fail with
    its own clear error."""
    fake_sub = _fake_subprocess_module(monkeypatch)
    # `check=False` already shields us from CalledProcessError; this
    # test pins that behavior so future refactors don't introduce
    # `check=True` and turn a benign reap failure into a launcher abort.
    fake_sub.run.return_value = MagicMock(returncode=1)

    handle = launch_worker_async(
        repo_root=tmp_path,
        prompt="hello",
        task_dir=tmp_path / "logs",
        image="rvh-test:latest",
        network="rvh-test-net",
        worker_endpoint="http://localhost:8001",
        worker_api_key="dummy",
        worker_model="m",
        container_name="rvh-test-w-foo-1",
    )

    assert handle is not None
    assert fake_sub.Popen.called, (
        "reaper failure aborted the launcher — Popen should still have been called"
    )
