"""Live sandbox check (needs Docker; opt in with RVH_DOCKER_TESTS=1).

A container on the run network — started with the same isolation flags as a
worker — must reach the LLM gateway and the CPG alias and nothing else: not
the internet, not the Docker host, not the upstream directly, and (when
RVH_EGRESS_LAN_URL is set) not the LAN. It must also see the repo read-only
with .rvh-runs masked.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid

import pytest

import image
import sandbox

pytestmark = pytest.mark.skipif(
    os.environ.get("RVH_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="set RVH_DOCKER_TESTS=1 to run live Docker sandbox checks",
)

PROBE = r"""
import json, os, socket, sys, urllib.error, urllib.request

def http(url):
    try:
        return urllib.request.urlopen(url, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None

def tcp(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close()
        return True
    except Exception:
        return False

upstream, lan = sys.argv[1], sys.argv[2]
print(json.dumps({
    "gateway": http("http://rvh-llm:8898/v1/models"),
    "cpg": tcp("rvh-cpg", 8080),
    "internet": http("https://pypi.org/simple/"),
    "docker_host": http("http://host.docker.internal:22/"),
    "upstream_direct": http(upstream + "/v1/models"),
    "lan": http(lan) if lan else None,
    "workspace_writable": os.access("/workspace", os.W_OK),
    "rvh_runs_listing": os.listdir("/workspace/.rvh-runs"),
}))
"""


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout.strip()


def test_worker_network_reaches_only_the_gateway_and_cpg(tmp_path):
    img = image.ensure_worker_image(log=print)
    prefix = f"rvh-egresstest-{uuid.uuid4().hex[:6]}"
    repo = tmp_path / "repo"
    (repo / ".rvh-runs").mkdir(parents=True)
    (repo / ".rvh-runs" / "state.db").write_text("secret")
    (repo / "app.py").write_text("print('hi')\n")
    try:
        network = sandbox.create_network(prefix)
        # Stand-in upstream on the default bridge, reachable only by the gateway.
        _docker("run", "-d", "--rm", "--name", f"{prefix}-upstream", "--entrypoint", "",
                img, "python3", "-m", "http.server", "9000")
        ip = _docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                     f"{prefix}-upstream")
        upstream = f"http://{ip}:9000"
        # Stand-in CPG sidecar, on the run network under its alias.
        _docker("run", "-d", "--rm", "--name", f"{prefix}-cpg", "--network", network,
                "--network-alias", sandbox.CPG_ALIAS, "--entrypoint", "",
                img, "python3", "-m", "http.server", "8080")
        sandbox.start_gateway(prefix, image=img, endpoint=upstream, api_key="k", network=network)

        out = _docker(
            "run", "--rm", "--network", network,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
            "--mount", f"type=bind,src={repo},dst=/workspace,readonly",
            "--mount", "type=tmpfs,dst=/workspace/.rvh-runs",
            "--entrypoint", "", img,
            "python3", "-c", PROBE, upstream, os.environ.get("RVH_EGRESS_LAN_URL", ""),
        )
        result = json.loads(out.splitlines()[-1])
    finally:
        sandbox.reclaim(prefix)

    assert result["gateway"] == 404          # proxied to the stand-in upstream (no /v1/models there)
    assert result["cpg"] is True
    assert result["internet"] is None
    assert result["docker_host"] is None
    assert result["upstream_direct"] is None
    assert result["lan"] is None
    assert result["workspace_writable"] is False
    assert result["rvh_runs_listing"] == []
