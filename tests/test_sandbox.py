"""scripts/sandbox.py — per-run network, gateway, and cleanup (docker mocked)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import sandbox


class FakeDocker:
    """Records docker argv; answers `exec` probes from a queue."""

    def __init__(self, probe=("200",), ps=""):
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.probe = list(probe)
        self.ps = ps

    def __call__(self, cmd, capture_output, text, timeout, env=None):
        self.calls.append(cmd)
        self.envs.append(env)
        out = ""
        if cmd[1] == "exec":
            out = self.probe.pop(0) if self.probe else "down"
        elif cmd[1] == "ps":
            out = self.ps
        return MagicMock(returncode=0, stdout=out + "\n", stderr="")


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    monkeypatch.setattr(sandbox.time, "sleep", lambda s: None)
    return fake


def test_run_prefix_is_stable_and_repo_specific(tmp_path):
    a, b = tmp_path / "SciTokens-CPP", tmp_path / "other" / "SciTokens-CPP"
    a.mkdir()
    b.mkdir(parents=True)
    assert sandbox.run_prefix(a) == sandbox.run_prefix(a)
    assert sandbox.run_prefix(a).startswith("rvh-scitokens-cpp-")
    assert sandbox.run_prefix(a) != sandbox.run_prefix(b)


def test_create_network_is_internal(docker):
    assert sandbox.create_network("rvh-x-abc123") == "rvh-x-abc123-net"
    assert docker.calls[0][:4] == ["docker", "network", "create", "--internal"]


def test_reclaim_removes_prefixed_containers_and_the_network(docker):
    docker.ps = "rvh-x-abc123-llm\nrvh-x-abc123-w-a.py-3"
    removed = sandbox.reclaim("rvh-x-abc123")
    assert removed == ["rvh-x-abc123-llm", "rvh-x-abc123-w-a.py-3"]
    assert docker.calls[0][docker.calls[0].index("--filter") + 1] == "name=^rvh-x-abc123-"
    assert ["docker", "rm", "-f", "rvh-x-abc123-llm"] in docker.calls
    assert docker.calls[-1] == ["docker", "network", "rm", "rvh-x-abc123-net"]


def test_gateway_keeps_the_key_out_of_argv(docker):
    sandbox.start_gateway("rvh-x", image="img", endpoint="http://e", api_key="s3cret", network="rvh-x-net")
    run = docker.calls[0]
    assert "s3cret" not in " ".join(run)
    assert run[run.index("LLM_API_KEY") - 1] == "-e"
    assert docker.envs[0]["LLM_API_KEY"] == "s3cret"
    assert docker.calls[1] == ["docker", "network", "connect", "--alias", "rvh-llm", "rvh-x-net", "rvh-x-llm"]


def test_gateway_waits_until_the_proxy_answers(docker):
    docker.probe = ["down", "down", "404"]  # 404: up, and the upstream answered
    sandbox.start_gateway("rvh-x", image="img", endpoint="http://e", api_key="k", network="n")
    assert sum(1 for c in docker.calls if c[1] == "exec") == 3


def test_gateway_that_cannot_reach_the_endpoint_fails_with_the_macos_hint(docker):
    docker.probe = ["502"]
    with pytest.raises(sandbox.SandboxError, match="Local Network"):
        sandbox.start_gateway("rvh-x", image="img", endpoint="http://e", api_key="k", network="n")


def test_gateway_that_never_listens_fails(docker):
    docker.probe = []
    with pytest.raises(sandbox.SandboxError, match="never started"):
        sandbox.start_gateway("rvh-x", image="img", endpoint="http://e", api_key="k", network="n", attempts=3)
