"""Per-run network sandbox for workers.

Each run gets an ``--internal`` Docker network (no route out). Workers join only
that network, where exactly two names answer:

* ``rvh-llm`` — the gateway container running the buffering LLM proxy. It is
  also on the default bridge, making it the network's only way out, and it
  forwards only to the configured endpoint (holding the real API key).
* ``rvh-cpg`` — the joern CPG sidecar.

Container and network names carry a per-run prefix, so runs on different repos
never collide and a killed run's leftovers can be found and removed.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

GATEWAY_ALIAS = "rvh-llm"
GATEWAY_PORT = 8898
CPG_ALIAS = "rvh-cpg"
CPG_PORT = 8080
WORKER_ENDPOINT = f"http://{GATEWAY_ALIAS}:{GATEWAY_PORT}"
WORKER_CPG_ENDPOINT = f"http://{CPG_ALIAS}:{CPG_PORT}"
# Workers never see the real API key; the gateway substitutes it.
WORKER_API_KEY = "sandboxed"
LOCAL_NETWORK_HINT = (
    "on macOS with a LAN endpoint, allow your container runtime (OrbStack or "
    "Docker Desktop) under System Settings → Privacy & Security → Local Network"
)


class SandboxError(RuntimeError):
    pass


def run_prefix(repo_root: Path) -> str:
    """``rvh-<repo-slug>-<6 hex of the repo path>``: stable per repo, distinct across repos."""
    slug = re.sub(r"[^a-z0-9_.-]", "-", repo_root.name.lower()).strip("-.") or "repo"
    digest = hashlib.sha1(str(repo_root.resolve()).encode()).hexdigest()[:6]
    return f"rvh-{slug}-{digest}"


def network_name(prefix: str) -> str:
    return f"{prefix}-net"


def gateway_name(prefix: str) -> str:
    return f"{prefix}-llm"


def cpg_name(prefix: str) -> str:
    return f"{prefix}-cpg"


def _docker(*args: str, check: bool = True, env: dict | None = None, timeout: int = 120):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, env=env)
    if check and r.returncode != 0:
        raise SandboxError(f"docker {' '.join(args[:2])} failed: {r.stderr.strip()[:500]}")
    return r


def reclaim(prefix: str) -> list[str]:
    """Remove every container and the network carrying this run's prefix.

    Runs at startup (a killed run leaves them behind) and at teardown.
    Returns the names of the containers removed.
    """
    listing = _docker("ps", "-a", "--filter", f"name=^{prefix}-", "--format", "{{.Names}}", check=False)
    names = listing.stdout.split()
    for name in names:
        _docker("rm", "-f", name, check=False)
    _docker("network", "rm", network_name(prefix), check=False)
    return names


def create_network(prefix: str) -> str:
    net = network_name(prefix)
    _docker("network", "create", "--internal", "--label", f"rvh.run={prefix}", net)
    return net


# Run inside the gateway: prints the HTTP status of /v1/models through the
# proxy, or "down" while the proxy isn't listening yet.
_GATEWAY_PROBE = (
    "import urllib.request, urllib.error\n"
    "try:\n"
    f"    print(urllib.request.urlopen('http://127.0.0.1:{GATEWAY_PORT}/v1/models', timeout=30).status)\n"
    "except urllib.error.HTTPError as e:\n"
    "    print(e.code)\n"
    "except Exception:\n"
    "    print('down')\n"
)


def start_gateway(
    prefix: str, *, image: str, endpoint: str, api_key: str, network: str, attempts: int = 30,
) -> None:
    """Run the buffering LLM proxy on the bridge plus the run network.

    The key is passed by name (``-e LLM_API_KEY``) so it never appears in argv.
    Raises SandboxError if the gateway can't reach ``endpoint``.
    """
    name = gateway_name(prefix)
    _docker(
        "run", "-d", "--rm", "--name", name,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-e", f"LLM_UPSTREAM={endpoint}", "-e", f"LLM_PROXY_PORT={GATEWAY_PORT}", "-e", "LLM_API_KEY",
        image, "python3", "/opt/rvh/llm_proxy.py",
        env=dict(os.environ, LLM_API_KEY=api_key),
    )
    _docker("network", "connect", "--alias", GATEWAY_ALIAS, network, name)
    for _ in range(attempts):
        status = _docker("exec", name, "python3", "-c", _GATEWAY_PROBE, check=False, timeout=60).stdout.strip()
        if status.isdigit():
            if int(status) < 500:
                return
            raise SandboxError(f"the LLM gateway can't reach {endpoint} (HTTP {status}); {LOCAL_NETWORK_HINT}")
        time.sleep(1)
    raise SandboxError("the LLM gateway container never started listening")
