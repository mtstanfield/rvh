"""scripts/preflight.py — fail fast before any work starts."""
import json
import urllib.error
from unittest.mock import MagicMock

import pytest

import preflight


class _Resp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _docker(monkeypatch, returncode=0, stderr=""):
    monkeypatch.setattr(
        preflight.subprocess, "run",
        lambda *a, **k: MagicMock(returncode=returncode, stdout="29.4.0\n", stderr=stderr),
    )


def _models(monkeypatch, ids):
    monkeypatch.setattr(
        preflight.urllib.request, "urlopen",
        lambda req, timeout: _Resp({"data": [{"id": i} for i in ids]}),
    )


def test_passes_when_docker_is_up_and_the_model_is_listed(monkeypatch):
    _docker(monkeypatch)
    _models(monkeypatch, ["m"])
    warnings = []
    preflight.run_preflight("http://e", "m", "k", warn=warnings.append)
    assert warnings == []


def test_warns_but_continues_when_the_model_is_not_listed(monkeypatch):
    _docker(monkeypatch)
    _models(monkeypatch, ["other"])
    warnings = []
    preflight.run_preflight("http://e", "m", "k", warn=warnings.append)
    assert len(warnings) == 1 and "'m'" in warnings[0]


def test_fails_when_the_docker_daemon_is_down(monkeypatch):
    _docker(monkeypatch, returncode=1, stderr="Cannot connect to the Docker daemon")
    with pytest.raises(preflight.PreflightError, match="Docker daemon"):
        preflight.run_preflight("http://e", "m", "k", warn=print)


def test_fails_when_docker_is_not_installed(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(preflight.subprocess, "run", missing)
    with pytest.raises(preflight.PreflightError, match="docker not found"):
        preflight.run_preflight("http://e", "m", "k", warn=print)


def test_fails_when_the_endpoint_is_unreachable(monkeypatch):
    _docker(monkeypatch)

    def refused(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(preflight.urllib.request, "urlopen", refused)
    with pytest.raises(preflight.PreflightError, match="cannot reach http://e/v1/models"):
        preflight.run_preflight("http://e", "m", "k", warn=print)


def test_sends_the_api_key(monkeypatch):
    _docker(monkeypatch)
    seen = {}

    def capture(req, timeout):
        seen["auth"] = req.get_header("Authorization")
        return _Resp({"data": [{"id": "m"}]})

    monkeypatch.setattr(preflight.urllib.request, "urlopen", capture)
    preflight.run_preflight("http://e", "m", "secret", warn=print)
    assert seen["auth"] == "Bearer secret"
