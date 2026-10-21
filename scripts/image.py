"""The rvh worker image: one image for every target, built once per
rvh version (its tag is a hash of everything that goes into it)."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
DOCKERFILE = SCRIPT_DIR.parent / "docker" / "worker.Dockerfile"
# Files the Dockerfile COPYs from the build context (scripts/).
IMAGE_FILES = ("cpg_client.py", "cpg_formatters.py", "cpg_mcp_server.py", "cpg_queries.py", "llm_proxy.py")
IMAGE_REPO = "rvh-worker"
BUILD_TIMEOUT_SECONDS = 1800


class ImageBuildError(RuntimeError):
    pass


def worker_image_tag() -> str:
    """rvh-worker:<12 hex>, hashed over the Dockerfile and every file it copies."""
    h = hashlib.sha256(DOCKERFILE.read_bytes())
    for name in IMAGE_FILES:
        h.update(name.encode() + b"\0")
        h.update((SCRIPT_DIR / name).read_bytes())
    return f"{IMAGE_REPO}:{h.hexdigest()[:12]}"


def image_exists(tag: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0


def ensure_worker_image(log: Callable[[str], None] = print) -> str:
    """Return the worker image tag, building the image first if it's missing."""
    tag = worker_image_tag()
    if image_exists(tag):
        return tag
    log(f"[image] building {tag} (once per rvh version)")
    try:
        r = subprocess.run(
            ["docker", "build", "-f", str(DOCKERFILE), "-t", tag, str(SCRIPT_DIR)],
            capture_output=True, text=True, timeout=BUILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImageBuildError(f"docker build timed out after {BUILD_TIMEOUT_SECONDS}s") from exc
    if r.returncode != 0:
        raise ImageBuildError(f"docker build failed:\n{r.stderr[-3000:]}")
    return tag
