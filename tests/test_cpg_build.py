"""Tests for scripts/cpg_build.py source hashing and rebuild detection."""
import hashlib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import cpg_build
from common import init_db, connect_db, utc_now


def _seed_files(run_dir: Path, paths: list[str]) -> None:
    """Insert rows into the files table for language-detect tests."""
    init_db(run_dir)
    now = utc_now()
    with connect_db(run_dir) as conn:
        for p in paths:
            ext = "." + p.rsplit(".", 1)[-1] if "." in p else ""
            conn.execute(
                """INSERT INTO files (path, rel_dir, extension, size_bytes,
                   is_binary, category, tags_json, excluded, created_at, updated_at)
                   VALUES (?, '', ?, 1, 0, 'code', '[]', 0, ?, ?)""",
                (p, ext, now, now),
            )


def test_detect_primary_language_python_dominant(tmp_path):
    _seed_files(tmp_path, [f"app/f{i}.py" for i in range(10)] + ["script.sh"])
    assert cpg_build.detect_primary_language(tmp_path) == "python"


def test_detect_primary_language_cpp_dominant(tmp_path):
    _seed_files(tmp_path, [f"src/f{i}.cpp" for i in range(5)]
                + ["Readme.md"] + ["one.py"])
    assert cpg_build.detect_primary_language(tmp_path) == "cpp"


def test_detect_primary_language_go(tmp_path):
    _seed_files(tmp_path, ["main.go", "server.go", "handler.go", "x.py"])
    assert cpg_build.detect_primary_language(tmp_path) == "go"


def test_detect_primary_language_counts_multi_dot_filenames(tmp_path):
    """Angular/NestJS files are named `x.module.ts`, `x.service.ts`: only the
    last suffix is the extension."""
    _seed_files(tmp_path, ["src/app.module.ts", "src/users.service.ts",
                           "src/users.controller.ts", "scripts/seed.py"])
    assert cpg_build.detect_primary_language(tmp_path) == "ts"


def test_detect_primary_language_fallback_empty_db(tmp_path):
    """Empty files table returns the fallback."""
    init_db(tmp_path)
    assert cpg_build.detect_primary_language(tmp_path) == "cpp"
    assert cpg_build.detect_primary_language(tmp_path, fallback="python") == "python"


def test_detect_primary_language_fallback_no_db(tmp_path):
    """Missing DB file → graceful fallback, not a crash."""
    assert cpg_build.detect_primary_language(tmp_path / "no-run-dir") == "cpp"


def test_detect_primary_language_skips_unknown_extensions(tmp_path):
    """Unknown ext (e.g. .json config dumped in code dir) shouldn't win —
    we skip past it to the next candidate."""
    _seed_files(tmp_path, ["a.json", "b.json", "c.json", "main.py"])
    assert cpg_build.detect_primary_language(tmp_path) == "python"


def test_hash_source_tree_deterministic(tmp_path):
    """Same content in same layout yields same hash twice."""
    (tmp_path / "a.cpp").write_text("int main() { return 0; }")
    (tmp_path / "b.h").write_text("#pragma once\nint f();")
    h1 = cpg_build.hash_source_tree(tmp_path)
    h2 = cpg_build.hash_source_tree(tmp_path)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_hash_source_tree_detects_content_change(tmp_path):
    """Changing file content changes the hash."""
    (tmp_path / "a.cpp").write_text("int main() { return 0; }")
    h1 = cpg_build.hash_source_tree(tmp_path)
    (tmp_path / "a.cpp").write_text("int main() { return 1; }")
    h2 = cpg_build.hash_source_tree(tmp_path)
    assert h1 != h2


def test_hash_source_tree_detects_new_file(tmp_path):
    """Adding a file changes the hash."""
    (tmp_path / "a.cpp").write_text("x")
    h1 = cpg_build.hash_source_tree(tmp_path)
    (tmp_path / "b.cpp").write_text("y")
    h2 = cpg_build.hash_source_tree(tmp_path)
    assert h1 != h2


def test_hash_source_tree_ignores_excluded_dirs(tmp_path):
    """Changes inside .git, node_modules, .rvh-runs do not affect hash."""
    (tmp_path / "a.cpp").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "objects").write_text("some git state")
    h1 = cpg_build.hash_source_tree(tmp_path)
    (tmp_path / ".git" / "objects").write_text("different git state")
    h2 = cpg_build.hash_source_tree(tmp_path)
    assert h1 == h2


def test_hash_source_tree_order_independent(tmp_path):
    """File enumeration order does not affect hash (walk is sorted)."""
    (tmp_path / "z.cpp").write_text("z")
    (tmp_path / "a.cpp").write_text("a")
    h = cpg_build.hash_source_tree(tmp_path)
    # Delete and recreate in different order
    (tmp_path / "z.cpp").unlink()
    (tmp_path / "a.cpp").unlink()
    (tmp_path / "a.cpp").write_text("a")
    (tmp_path / "z.cpp").write_text("z")
    assert cpg_build.hash_source_tree(tmp_path) == h


def test_should_rebuild_missing_artifact(tmp_path):
    """Missing artifact means rebuild."""
    cpg_dir = tmp_path / "cpg"
    assert cpg_build.should_rebuild(
        cpg_dir=cpg_dir, lang="cpp", source_hash="abc", joern_version="v1"
    ) is True


def test_should_rebuild_missing_metadata(tmp_path):
    """Artifact present but no metadata means rebuild."""
    cpg_dir = tmp_path / "cpg"
    cpg_dir.mkdir()
    (cpg_dir / "cpp.bin").write_bytes(b"fake")
    assert cpg_build.should_rebuild(
        cpg_dir=cpg_dir, lang="cpp", source_hash="abc", joern_version="v1"
    ) is True


def test_should_rebuild_source_hash_mismatch(tmp_path):
    cpg_dir = tmp_path / "cpg"
    cpg_build.write_metadata(
        cpg_dir, lang="cpp", source_hash="OLD", joern_version="v1",
        build_duration_ms=1000,
    )
    (cpg_dir / "cpp.bin").write_bytes(b"fake")
    assert cpg_build.should_rebuild(
        cpg_dir=cpg_dir, lang="cpp", source_hash="NEW", joern_version="v1"
    ) is True


def test_should_rebuild_joern_version_mismatch(tmp_path):
    cpg_dir = tmp_path / "cpg"
    cpg_build.write_metadata(
        cpg_dir, lang="cpp", source_hash="abc", joern_version="v1",
        build_duration_ms=1000,
    )
    (cpg_dir / "cpp.bin").write_bytes(b"fake")
    assert cpg_build.should_rebuild(
        cpg_dir=cpg_dir, lang="cpp", source_hash="abc", joern_version="v2"
    ) is True


def test_should_not_rebuild_when_all_match(tmp_path):
    cpg_dir = tmp_path / "cpg"
    cpg_build.write_metadata(
        cpg_dir, lang="cpp", source_hash="abc", joern_version="v1",
        build_duration_ms=1000,
    )
    (cpg_dir / "cpp.bin").write_bytes(b"fake")
    assert cpg_build.should_rebuild(
        cpg_dir=cpg_dir, lang="cpp", source_hash="abc", joern_version="v1"
    ) is False


def test_get_joern_version_parses_output():
    with patch("cpg_build.shutil.which", return_value="/usr/local/bin/docker"), \
         patch("subprocess.run") as sr:
        sr.return_value = MagicMock(
            returncode=0,
            stdout="sha256:abcdef123456\n",
            stderr="",
        )
        v = cpg_build.get_joern_version()
    # Shape is "<image>@<id>" so rebuild-detection invalidates when the
    # image digest changes on pull.
    assert v.endswith("@sha256:abcdef123456")
    assert "ghcr.io/joernio/joern" in v


def test_get_joern_version_raises_when_missing():
    # When `docker` isn't on PATH, shutil.which returns None and we raise early.
    with patch("cpg_build.shutil.which", return_value=None):
        with pytest.raises(cpg_build.CpgBuildError) as exc:
            cpg_build.get_joern_version()
    assert "docker not found" in str(exc.value).lower() or "not found" in str(exc.value).lower()


def test_build_cpg_invokes_correct_frontend(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / "a.cpp").write_text("int main(){return 0;}")
    cpg_dir = tmp_path / "cpg"
    with patch("subprocess.run") as sr:
        sr.return_value = MagicMock(returncode=0, stdout="", stderr="")
        cpg_build.run_joern_parse(
            source_root=src, output=cpg_dir / "cpp.bin", lang="cpp", timeout_sec=60
        )
    call_args = sr.call_args[0][0]
    # Must shell out to docker with the joernio/joern image.
    assert call_args[0] == "docker"
    assert any("joernio/joern" in a for a in call_args)
    # And ask for the c frontend somewhere in argv.
    assert any(a == "c" for a in call_args)


def test_build_cpg_raises_on_nonzero_exit(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    cpg_dir = tmp_path / "cpg"
    with patch("subprocess.run") as sr:
        sr.return_value = MagicMock(returncode=1, stdout="", stderr="parse error")
        with pytest.raises(cpg_build.CpgBuildError):
            cpg_build.run_joern_parse(
                source_root=src, output=cpg_dir / "cpp.bin", lang="cpp", timeout_sec=60
            )


# ---------------------------------------------------------------------------
# anchor_supported_by_cpg — language gating for per-anchor CPG exposure
# ---------------------------------------------------------------------------

def _seed_cpg(cpg_dir: Path, lang: str) -> None:
    cpg_build.write_metadata(
        cpg_dir, lang=lang, source_hash="abc", joern_version="v1",
        build_duration_ms=100,
    )
    (cpg_dir / f"{lang}.bin").write_bytes(b"fake")


def test_anchor_supported_python_anchor_python_cpg(tmp_path):
    cpg_dir = tmp_path / "cpg"
    _seed_cpg(cpg_dir, "python")
    assert cpg_build.anchor_supported_by_cpg("api/views.py", cpg_dir) is True


def test_anchor_supported_shell_anchor_python_cpg(tmp_path):
    """Shell anchor in Python-only CPG: caller should skip CPG exposure."""
    cpg_dir = tmp_path / "cpg"
    _seed_cpg(cpg_dir, "python")
    assert cpg_build.anchor_supported_by_cpg("killServices.sh", cpg_dir) is False


def test_anchor_supported_header_maps_to_cpp(tmp_path):
    """`.h` is treated as C++ per _EXT_TO_CPG_LANG."""
    cpg_dir = tmp_path / "cpg"
    _seed_cpg(cpg_dir, "cpp")
    assert cpg_build.anchor_supported_by_cpg("src/foo.h", cpg_dir) is True


def test_anchor_supported_vue_anchor_js_cpg(tmp_path):
    """jssrc2cpg parses Vue single-file components into the JS CPG."""
    cpg_dir = tmp_path / "cpg"
    _seed_cpg(cpg_dir, "js")
    assert cpg_build.anchor_supported_by_cpg("src/components/App.vue", cpg_dir) is True


def test_anchor_supported_python_anchor_cpp_cpg(tmp_path):
    """Cross-language: anchor is .py but CPG built for cpp → not supported."""
    cpg_dir = tmp_path / "cpg"
    _seed_cpg(cpg_dir, "cpp")
    assert cpg_build.anchor_supported_by_cpg("script.py", cpg_dir) is False


def test_anchor_supported_no_metadata(tmp_path):
    """Missing metadata.json (CPG never built) → unsupported for any anchor."""
    cpg_dir = tmp_path / "cpg"
    cpg_dir.mkdir()
    assert cpg_build.anchor_supported_by_cpg("api/views.py", cpg_dir) is False


def test_anchor_supported_missing_cpg_dir(tmp_path):
    assert cpg_build.anchor_supported_by_cpg("api/views.py", tmp_path / "no-cpg") is False


def test_anchor_supported_no_extension(tmp_path):
    cpg_dir = tmp_path / "cpg"
    _seed_cpg(cpg_dir, "python")
    assert cpg_build.anchor_supported_by_cpg("Dockerfile", cpg_dir) is False


def test_anchor_supported_unknown_extension(tmp_path):
    cpg_dir = tmp_path / "cpg"
    _seed_cpg(cpg_dir, "python")
    assert cpg_build.anchor_supported_by_cpg("README.md", cpg_dir) is False
    assert cpg_build.anchor_supported_by_cpg("config.yaml", cpg_dir) is False


def test_anchor_supported_typescript_javascript_share_frontend(tmp_path):
    """js + ts share the jssrc frontend, so a .ts anchor is supported by a js CPG."""
    cpg_dir = tmp_path / "cpg"
    _seed_cpg(cpg_dir, "js")
    assert cpg_build.anchor_supported_by_cpg("frontend/app.ts", cpg_dir) is True
    assert cpg_build.anchor_supported_by_cpg("frontend/app.tsx", cpg_dir) is True
    assert cpg_build.anchor_supported_by_cpg("frontend/app.js", cpg_dir) is True
