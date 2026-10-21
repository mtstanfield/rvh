"""Tests for worker.terminate_worker — graceful-then-forceful teardown that
also removes the docker container.

The bug this exists to prevent: SIGKILL on the host-side `docker run` CLI
process does NOT propagate to the container. With LM Studio configured
for one slot, leaked goose processes inside orphaned containers starved
every subsequent worker. Observed on coriolix-3.0.1 overnight run
2026-04-30: one budget timeout cascaded into 12 consecutive timeouts,
each with stdout=120 bytes (goose banner only), because each new worker
queued behind orphan traffic. terminate_worker centralises the fix by
adding `docker rm -f <name>` to the kill path.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from worker import launch_worker_async, terminate_worker


@pytest.fixture
def container_config():
    return {"image_tag": "rvh-test:latest"}


def _fake_subprocess_module(monkeypatch):
    fake_subprocess = MagicMock()
    fake_subprocess.DEVNULL = -3
    fake_subprocess.PIPE = -1
    fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
    fake_proc = MagicMock()
    fake_proc.stdin = MagicMock()
    fake_proc.poll.return_value = None
    fake_subprocess.Popen.return_value = fake_proc
    fake_subprocess.run.return_value = MagicMock(returncode=0)
    monkeypatch.setattr("worker.subprocess", fake_subprocess)
    return fake_subprocess


def test_handle_includes_container_name(tmp_path, container_config, monkeypatch):
    """The handle returned by launch_worker_async must carry container_name
    forward so terminate_worker can target the right container."""
    _fake_subprocess_module(monkeypatch)
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
    assert handle["container_name"] == "rvh-test-w-foo-1"


def test_terminate_worker_terminates_then_removes_container(monkeypatch):
    """Happy path: SIGTERM, container exits within grace, then `docker rm -f`
    runs as belt-and-braces (covers the case where goose hung mid-stream
    and SIGTERM was forwarded but the container is still up)."""
    fake_sub = _fake_subprocess_module(monkeypatch)
    proc = MagicMock()
    proc.poll.return_value = None  # still running when terminate is called
    proc.wait.return_value = 0  # exits cleanly during grace
    handle = {
        "process": proc,
        "container_name": "rvh-test-w-foo-1",
        "dry_run": False,
    }

    terminate_worker(handle, grace_seconds=10)

    proc.terminate.assert_called_once()
    proc.wait.assert_called_once_with(timeout=10)
    proc.kill.assert_not_called()
    rm_calls = [
        c for c in fake_sub.run.call_args_list
        if c.args and isinstance(c.args[0], list) and c.args[0][:3] == ["docker", "rm", "-f"]
    ]
    assert rm_calls, "docker rm -f must run after terminate"
    assert rm_calls[0].args[0] == ["docker", "rm", "-f", "rvh-test-w-foo-1"]
    assert rm_calls[0].kwargs.get("check") is False


def test_terminate_worker_kills_then_removes_when_grace_expires(monkeypatch):
    """If SIGTERM doesn't drain within the grace window we SIGKILL the CLI,
    then we MUST still `docker rm -f` because SIGKILL on the docker-run
    CLI doesn't touch the container — the orphan-leak bug."""
    fake_sub = _fake_subprocess_module(monkeypatch)
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)
    handle = {
        "process": proc,
        "container_name": "rvh-test-w-foo-1",
        "dry_run": False,
    }

    terminate_worker(handle, grace_seconds=10)

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    rm_calls = [
        c for c in fake_sub.run.call_args_list
        if c.args and isinstance(c.args[0], list) and c.args[0][:3] == ["docker", "rm", "-f"]
    ]
    assert rm_calls, "docker rm -f MUST run after SIGKILL — this is the bug fix"
    assert rm_calls[0].args[0] == ["docker", "rm", "-f", "rvh-test-w-foo-1"]


def test_terminate_worker_no_container_name_skips_docker_rm(monkeypatch):
    """Subprocess-mode workers (no container) shouldn't trigger docker rm."""
    fake_sub = _fake_subprocess_module(monkeypatch)
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.return_value = 0
    handle = {
        "process": proc,
        "container_name": None,
        "dry_run": False,
    }

    terminate_worker(handle, grace_seconds=10)

    proc.terminate.assert_called_once()
    rm_calls = [
        c for c in fake_sub.run.call_args_list
        if c.args and isinstance(c.args[0], list) and c.args[0][:3] == ["docker", "rm", "-f"]
    ]
    assert rm_calls == [], "no container_name → no docker rm"


def test_terminate_worker_skips_terminate_when_proc_already_exited(monkeypatch):
    """If the proc finished between the budget-check and our terminate
    call, don't double-signal — but still reap the container in case the
    CLI exited before --rm fired (rare, but cheap insurance)."""
    fake_sub = _fake_subprocess_module(monkeypatch)
    proc = MagicMock()
    proc.poll.return_value = 0  # already exited
    handle = {
        "process": proc,
        "container_name": "rvh-test-w-foo-1",
        "dry_run": False,
    }

    terminate_worker(handle, grace_seconds=10)

    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()
    rm_calls = [
        c for c in fake_sub.run.call_args_list
        if c.args and isinstance(c.args[0], list) and c.args[0][:3] == ["docker", "rm", "-f"]
    ]
    assert rm_calls, "container reap must run even when proc already exited"
