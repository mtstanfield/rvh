"""End-to-end pipeline test: build CPG, start sidecar, query, stop.

Gated on Docker being available locally (image joernio/joern).
Skipped in CI without Docker.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

import cpg_build
import cpg_sidecar
from cpg_client import CpgClient
import cpg_queries as q


def _docker_available():
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


_REQUIRES_DOCKER = pytest.mark.skipif(
    not _docker_available(),
    reason=(
        "requires Docker daemon running (and `docker pull joernio/joern:latest`) "
        "— see docs/cpg-host-setup.md"
    ),
)


FIXTURE = Path(__file__).parent / "fixtures" / "tiny_cpp"


@_REQUIRES_DOCKER
def test_end_to_end_cpp(tmp_path):
    # Phase 4a: build CPG
    cpg_dir = tmp_path / "cpg"
    result = cpg_build.build_cpg(
        source_root=FIXTURE, cpg_dir=cpg_dir, lang="cpp", timeout_sec=120,
    )
    assert result["rebuilt"] is True
    assert (cpg_dir / "cpp.bin").exists()

    # Phase 4b: start sidecar
    port_file = tmp_path / "sidecar.port"
    sc = cpg_sidecar.start(cpg_path=cpg_dir / "cpp.bin", port_file=port_file)
    try:
        client = CpgClient(sc.endpoint)

        # Query 1: entrypoints — must include "main"
        rows = client.query(q.build_entrypoints())
        assert any("main" in str(r.get("name", "")) for r in rows), (
            f"entrypoints should include main; got: {rows!r}"
        )

        # Query 2: xref on a known dangerous symbol — must locate strcpy
        rows = client.query(q.build_xref(symbol="strcpy"))
        assert len(rows) >= 1, f"xref(strcpy) should match in tiny_cpp; got: {rows!r}"

        # Query 3: func lookup on main — should return a match
        rows_main = client.query(q.build_func(name="main"))
        assert len(rows_main) >= 1, f"func(main) should match; got: {rows_main!r}"

    finally:
        # Phase 7: teardown — start() already persisted the .container-id file.
        cpg_sidecar.stop(port_file=port_file)


def test_mcp_server_lists_all_eight_tools():
    """Spawn cpg_mcp_server.py over stdio, request tools/list, verify all
    8 tools are advertised with non-empty descriptions."""
    import asyncio
    import sys as _sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        # Spawn with the same interpreter running the tests — ensures the
        # venv's installed `mcp` module is importable by the subprocess.
        server_params = StdioServerParameters(
            command=_sys.executable,
            args=[str(Path(__file__).parent.parent / "scripts" / "cpg_mcp_server.py")],
            env={"RVH_CPG_ENDPOINT": "http://127.0.0.1:0", "PYTHONUNBUFFERED": "1"},
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return tools.tools

    tool_list = asyncio.run(run())
    names = {t.name for t in tool_list}
    assert names == {
        "paths", "reachable",
        "callers", "callees", "xref", "func", "inspect", "entrypoints",
    }, f"unexpected tool set: {names}"
    # Every tool must have a non-empty description — gemma4 reads these
    # at tool-choice time, so empty descriptions mean silent under-use.
    for t in tool_list:
        assert t.description and len(t.description) > 20, (
            f"tool {t.name!r} has weak or missing description"
        )
