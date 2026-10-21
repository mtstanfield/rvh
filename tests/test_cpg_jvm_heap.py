"""The joern JVM heap is capped via JAVA_TOOL_OPTIONS on the docker run, so large
CPGs don't OOM the build (joern-parse) or the sidecar (joern --server). Sized by
RVH_CPG_HEAP_GB (default 16); applies to both because JAVA_TOOL_OPTIONS is read by
the JVM itself."""
from __future__ import annotations

import cpg_build
import cpg_sidecar


def test_default_heap_args(monkeypatch):
    monkeypatch.delenv("RVH_CPG_HEAP_GB", raising=False)
    assert cpg_build.cpg_jvm_docker_args() == ["-e", "JAVA_TOOL_OPTIONS=-Xmx16g"]


def test_heap_args_env_override(monkeypatch):
    monkeypatch.setenv("RVH_CPG_HEAP_GB", "48")
    assert cpg_build.cpg_jvm_docker_args() == ["-e", "JAVA_TOOL_OPTIONS=-Xmx48g"]


def test_build_cmd_includes_heap_before_image(monkeypatch, tmp_path):
    monkeypatch.setenv("RVH_CPG_HEAP_GB", "20")
    captured = {}

    class _R:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(cpg_build.subprocess, "run",
                        lambda cmd, **kw: (captured.__setitem__("cmd", cmd), _R())[1])
    (tmp_path / "src").mkdir()
    cpg_build.run_joern_parse(
        source_root=tmp_path / "src", output=tmp_path / "out" / "python.bin",
        lang="python", timeout_sec=60,
    )
    cmd = captured["cmd"]
    assert "JAVA_TOOL_OPTIONS=-Xmx20g" in cmd
    # heap is a docker-run option → must precede the image
    assert cmd.index("JAVA_TOOL_OPTIONS=-Xmx20g") < cmd.index(cpg_build.JOERN_IMAGE)


def test_sidecar_cmd_includes_heap_before_image(monkeypatch, tmp_path):
    monkeypatch.setenv("RVH_CPG_HEAP_GB", "24")
    captured = {}

    class _R:
        returncode = 0
        stdout = "container123\n"
        stderr = ""

    monkeypatch.setattr(cpg_sidecar.subprocess, "run",
                        lambda cmd, **kw: (captured.__setitem__("cmd", cmd), _R())[1])
    monkeypatch.setattr(cpg_sidecar, "pick_free_port", lambda: 54321)
    monkeypatch.setattr(cpg_sidecar, "health_check", lambda endpoint, **kw: True)
    (tmp_path / "cpp.bin").write_bytes(b"x")
    cpg_sidecar.start(cpg_path=tmp_path / "cpp.bin", port_file=tmp_path / "sidecar.port")
    cmd = captured["cmd"]
    assert "JAVA_TOOL_OPTIONS=-Xmx24g" in cmd
    assert cmd.index("JAVA_TOOL_OPTIONS=-Xmx24g") < cmd.index(cpg_sidecar.JOERN_IMAGE)
