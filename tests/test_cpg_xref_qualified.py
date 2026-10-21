"""xref on qualified names (subprocess.run, yaml.load) against a real CPG.

joern keeps a call's short name in `name` ("run") and the qualified form only
in `code` ("subprocess.run(...)") and `methodFullName`; a name-only match can
never find "subprocess.run", so workers were told such sinks don't exist.
Needs Docker (builds a CPG with the pinned joern image).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

import cpg_build
import cpg_queries as q
import cpg_sidecar
from cpg_client import CpgClient

FIXTURE = Path(__file__).parent / "fixtures" / "qualified_calls_python"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="requires a running Docker daemon")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cpg")
    cpg_build.build_cpg(source_root=FIXTURE, cpg_dir=tmp / "cpg", lang="python", timeout_sec=300)
    port_file = tmp / "sidecar.port"
    sc = cpg_sidecar.start(cpg_path=tmp / "cpg" / "python.bin", port_file=port_file)
    try:
        yield CpgClient(sc.endpoint)
    finally:
        cpg_sidecar.stop(port_file=port_file)


def _call_sites(rows) -> set[tuple[str, int]]:
    return {(r["file"], r["line"]) for r in rows if r["label"] == "CALL"}


def test_qualified_symbol_finds_its_call_sites(client):
    assert ("app.py", 6) in _call_sites(client.query(q.build_xref(symbol="subprocess.run")))
    assert ("app.py", 10) in _call_sites(client.query(q.build_xref(symbol="yaml.load")))


def test_qualified_symbol_excludes_same_name_on_other_receivers(client):
    assert ("app.py", 20) not in _call_sites(client.query(q.build_xref(symbol="subprocess.run")))


def test_unqualified_symbol_still_matches_every_call_of_that_name(client):
    sites = _call_sites(client.query(q.build_xref(symbol="run")))
    assert {("app.py", 6), ("app.py", 20)} <= sites
