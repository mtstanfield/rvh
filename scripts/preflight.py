"""Fail-fast startup checks: Docker answers and the model endpoint is reachable."""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from typing import Callable


class PreflightError(RuntimeError):
    """A prerequisite is missing; the message says which."""


def check_docker() -> None:
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as exc:
        raise PreflightError("docker not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise PreflightError("`docker info` timed out — is the Docker daemon running?") from exc
    if r.returncode != 0:
        raise PreflightError(f"Docker daemon not reachable: {r.stderr.strip()[:300]}")


def list_models(endpoint: str, api_key: str, *, timeout: float = 20.0) -> list[str]:
    """Model ids from ``<endpoint>/v1/models``."""
    url = endpoint + "/v1/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise PreflightError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PreflightError(f"cannot reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PreflightError(f"{url} did not return JSON") from exc
    return [m.get("id", "") for m in payload.get("data", []) if isinstance(m, dict)]


def run_preflight(endpoint: str, model: str, api_key: str, *, warn: Callable[[str], None]) -> None:
    """Raise PreflightError if Docker or the endpoint is unusable.

    A model missing from ``/v1/models`` only warns: some servers serve aliases
    they don't list.
    """
    check_docker()
    models = list_models(endpoint, api_key)
    if model not in models:
        listed = ", ".join(models[:8]) or "none"
        warn(f"WARNING: {model!r} is not listed by {endpoint}/v1/models (listed: {listed}); continuing")
