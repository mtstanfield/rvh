"""Tests for joern --server sidecar lifecycle."""
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests_mock

import cpg_sidecar


def test_pick_free_port_returns_int():
    port = cpg_sidecar.pick_free_port()
    assert isinstance(port, int)
    assert 1024 < port < 65536


def test_health_check_ok():
    with requests_mock.Mocker() as m:
        m.get("http://localhost:8080/", status_code=200)
        assert cpg_sidecar.health_check("http://localhost:8080", attempts=1) is True


def test_health_check_fails_after_timeout():
    with requests_mock.Mocker() as m:
        m.get("http://localhost:8080/", status_code=503)
        assert cpg_sidecar.health_check(
            "http://localhost:8080", attempts=2, interval_sec=0
        ) is False


def test_start_writes_port_file(tmp_path):
    port_file = tmp_path / "sidecar.port"
    fake_run = MagicMock(returncode=0, stdout="abc123container\n", stderr="")
    with patch("subprocess.run", return_value=fake_run), \
         patch("cpg_sidecar.health_check", return_value=True), \
         patch("cpg_sidecar.pick_free_port", return_value=54321):
        sc = cpg_sidecar.start(
            cpg_path=tmp_path / "cpp.bin", port_file=port_file
        )
    assert port_file.read_text().strip() == "54321"
    assert sc.port == 54321
    assert sc.container_id == "abc123container"
    # The .container-id sibling file is written by start() itself.
    cid_file = port_file.with_suffix(".container-id")
    assert cid_file.read_text().strip() == "abc123container"


def test_start_raises_on_health_check_fail(tmp_path):
    port_file = tmp_path / "sidecar.port"
    fake_run = MagicMock(returncode=0, stdout="abc123container\n", stderr="")
    with patch("subprocess.run", return_value=fake_run), \
         patch("cpg_sidecar.health_check", return_value=False), \
         patch("cpg_sidecar.pick_free_port", return_value=54321):
        with pytest.raises(cpg_sidecar.SidecarError):
            cpg_sidecar.start(cpg_path=tmp_path / "cpp.bin", port_file=port_file)


def test_start_joins_the_run_network_under_its_alias(tmp_path):
    fake_run = MagicMock(returncode=0, stdout="abc123container\n", stderr="")
    with patch("subprocess.run", return_value=fake_run) as run, \
         patch("cpg_sidecar.health_check", return_value=True), \
         patch("cpg_sidecar.pick_free_port", return_value=54321):
        cpg_sidecar.start(
            cpg_path=tmp_path / "cpp.bin", port_file=tmp_path / "sidecar.port",
            name="rvh-x-cpg", network="rvh-x-net",
        )
    docker_run, connect = run.call_args_list[0].args[0], run.call_args_list[1].args[0]
    assert docker_run[docker_run.index("--name") + 1] == "rvh-x-cpg"
    assert connect == ["docker", "network", "connect", "--alias", "rvh-cpg", "rvh-x-net", "abc123container"]


def test_start_stops_the_container_if_the_network_join_fails(tmp_path):
    results = [MagicMock(returncode=0, stdout="abc123container\n", stderr=""),
               MagicMock(returncode=1, stdout="", stderr="no such network"),
               MagicMock(returncode=0, stdout="", stderr="")]
    with patch("subprocess.run", side_effect=results) as run, \
         patch("cpg_sidecar.health_check", return_value=True), \
         patch("cpg_sidecar.pick_free_port", return_value=54321):
        with pytest.raises(cpg_sidecar.SidecarError, match="network connect"):
            cpg_sidecar.start(cpg_path=tmp_path / "cpp.bin", port_file=tmp_path / "sidecar.port",
                              network="rvh-x-net")
    assert run.call_args_list[2].args[0] == ["docker", "stop", "abc123container"]


def test_start_waits_long_enough_for_a_large_cpg_to_load(tmp_path):
    """A 72 MB PHP CPG sometimes took >60 s to serve; the 30-poll budget dropped
    two comanage pins to no-CPG mid-eval (2026-09-24 full-v1 baseline)."""
    fake_run = MagicMock(returncode=0, stdout="abc123container\n", stderr="")
    with patch("subprocess.run", return_value=fake_run), \
         patch("cpg_sidecar.health_check", return_value=True) as hc, \
         patch("cpg_sidecar.pick_free_port", return_value=54321):
        cpg_sidecar.start(cpg_path=tmp_path / "php.bin", port_file=tmp_path / "sidecar.port")
    kwargs = hc.call_args.kwargs
    assert kwargs.get("attempts", 30) * kwargs.get("interval_sec", 1.0) >= 300
