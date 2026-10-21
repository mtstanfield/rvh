"""Lifecycle manager for the joern --server sidecar container."""
from __future__ import annotations

import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from cpg_build import JOERN_IMAGE, cpg_jvm_docker_args


class SidecarError(RuntimeError):
    pass


@dataclass
class Sidecar:
    pid: int  # vestigial: always 0 for Docker-backed sidecars; kept for bw-compat
    port: int
    endpoint: str
    container_id: str = ""


def pick_free_port() -> int:
    """OS-assigned free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Seconds to wait for joern --server to load a CPG and answer. Returns as soon
# as it does; large CPGs (a 72 MB PHP graph) can take over a minute.
SIDECAR_HEALTH_SECONDS = 300


def health_check(endpoint: str, *, attempts: int = 30, interval_sec: float = 1.0) -> bool:
    """Poll endpoint until the server responds at all, or give up.

    Joern's server returns 404 for GET / (no handler at root) but the
    process is healthy — any non-5xx response counts as alive.
    """
    for _ in range(attempts):
        try:
            r = requests.get(endpoint + "/", timeout=1.0)
            if r.status_code < 500:
                return True
        except requests.RequestException:
            pass
        time.sleep(interval_sec)
    return False


def start(
    *,
    cpg_path: Path,
    port_file: Path,
    name: str | None = None,
    network: str | None = None,
    network_alias: str = "rvh-cpg",
) -> Sidecar:
    """Spawn joern --server inside a joernio/joern container, health-check, persist state.

    The server is published on 127.0.0.1 for host-side queries (decomposition).
    With ``network``, the container also joins that Docker network as
    ``network_alias`` so sandboxed workers can reach it on port 8080.

    Raises SidecarError if the server does not come up within the health budget.

    Writes two files next to `port_file`:
      - port_file itself — the host-side port
      - port_file.with_suffix('.container-id') — the docker container id
    """
    port = pick_free_port()
    endpoint = f"http://127.0.0.1:{port}"
    cpg_abs = Path(cpg_path).resolve()
    cid_file = port_file.with_suffix(".container-id")
    cmd = ["docker", "run", "-d", "--rm"]
    if name:
        cmd += ["--name", name]
    cmd += [
        *cpg_jvm_docker_args(),
        "-v", f"{cpg_abs}:/cpg/cpg.bin:ro",
        "-p", f"127.0.0.1:{port}:8080",
        JOERN_IMAGE,
        "joern", "--server",
        "--server-host", "0.0.0.0",
        "--server-port", "8080",
        "--import-cpg", "/cpg/cpg.bin",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise SidecarError(f"docker not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise SidecarError(f"docker run timed out: {e}") from e
    if r.returncode != 0:
        raise SidecarError(f"docker run failed: {r.stderr[:500]}")
    container_id = r.stdout.strip()
    if not container_id:
        raise SidecarError("docker run returned no container id")
    if not health_check(endpoint, attempts=SIDECAR_HEALTH_SECONDS, interval_sec=1.0):
        subprocess.run(
            ["docker", "stop", container_id],
            capture_output=True, timeout=30,
        )
        raise SidecarError(
            f"joern --server did not come up within health budget (port {port})"
        )
    if network:
        r = subprocess.run(
            ["docker", "network", "connect", "--alias", network_alias, network, container_id],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            subprocess.run(["docker", "stop", container_id], capture_output=True, timeout=30)
            raise SidecarError(f"docker network connect failed: {r.stderr[:500]}")
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(port))
    cid_file.write_text(container_id)
    return Sidecar(pid=0, port=port, endpoint=endpoint, container_id=container_id)


def stop(*, port_file: Path) -> None:
    """Terminate the sidecar container. Idempotent."""
    cid_file = port_file.with_suffix(".container-id")
    if not cid_file.exists():
        return
    try:
        container_id = cid_file.read_text().strip()
    except (OSError, FileNotFoundError):
        return
    if container_id:
        subprocess.run(
            ["docker", "stop", container_id],
            capture_output=True, timeout=30,
        )
    # container has --rm so it self-removes on stop
    for p in (cid_file, port_file):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    import argparse
    import json
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="action", required=True)
    s1 = sub.add_parser("start")
    s1.add_argument("--cpg", required=True); s1.add_argument("--port-file", required=True)
    s2 = sub.add_parser("stop")
    s2.add_argument("--port-file", required=True)
    args = p.parse_args()
    if args.action == "start":
        sc = start(cpg_path=Path(args.cpg), port_file=Path(args.port_file))
        # start() already writes the .container-id file; nothing more to persist.
        print(json.dumps({
            "container_id": sc.container_id,
            "port": sc.port,
            "endpoint": sc.endpoint,
        }))
    else:
        stop(port_file=Path(args.port_file))
