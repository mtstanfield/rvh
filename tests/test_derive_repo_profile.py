"""Tests for scripts/derive_repo_profile.py.

Regression for the coriolix run where repo_profile.json was empty
(all fields []) because no orchestration step populated it after
init_run wrote the bootstrap stub.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _run_subprocess(cmd: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS_DIR)}
    return subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)


def _make_django_repo(repo_root: Path) -> None:
    """A coriolix-shaped repo: Django manage.py + production_paths
    (gui/, dyncx/, api/) + tests/."""
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "manage.py").write_text("import django\n")
    (repo_root / "gui").mkdir()
    (repo_root / "gui" / "views.py").write_text("def view(): pass\n")
    (repo_root / "gui" / "urls.py").write_text("urlpatterns = []\n")
    (repo_root / "dyncx").mkdir()
    (repo_root / "dyncx" / "models.py").write_text("from django.db import models\n")
    (repo_root / "api").mkdir()
    (repo_root / "api" / "serializers.py").write_text("import rest\n")
    (repo_root / "tests").mkdir()
    (repo_root / "tests" / "test_x.py").write_text("def test_x(): pass\n")


def _init_run(repo_root: Path, run_dir: Path) -> None:
    _run_subprocess([
        sys.executable, str(SCRIPTS_DIR / "init_run.py"),
        "--repo", str(repo_root),
        "--run-dir", str(run_dir),
    ])


def _seed_files(run_dir: Path, files: list[tuple[str, str, bool]]) -> None:
    """Insert (rel_path, extension, is_excluded) rows so derive can count
    languages. Bypasses inventory_repo for hermeticity."""
    db = run_dir / "state.db"
    conn = sqlite3.connect(db)
    try:
        for rel_path, extension, excluded in files:
            conn.execute(
                "INSERT INTO files (path, rel_dir, extension, size_bytes, "
                "is_binary, category, tags_json, excluded, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rel_path, str(Path(rel_path).parent) or "/", extension,
                 100, 0, "code", "[]", 1 if excluded else 0,
                 "2026-04-19T00:00:00+00:00", "2026-04-19T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()


def _write_analysis(run_dir: Path, payload: dict) -> None:
    (run_dir / "repo_analysis.json").write_text(json.dumps(payload))


def test_derive_populates_django_profile_end_to_end(tmp_path):
    """Coriolix-like repo: manage.py + production_paths + a few py files
    → derived profile has python language, django + docker frameworks
    (depending on what marker files exist), Django entrypoints, and
    hot/cold path prefixes."""
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    _make_django_repo(repo_root)
    _init_run(repo_root, run_dir)
    _seed_files(run_dir, [
        ("gui/views.py", ".py", False),
        ("gui/urls.py", ".py", False),
        ("dyncx/models.py", ".py", False),
        ("api/serializers.py", ".py", False),
        ("tests/test_x.py", ".py", False),
        ("blob.bin", "", True),  # excluded; must not contribute to languages
    ])
    _write_analysis(run_dir, {
        "schema_version": 1,
        "production_paths": ["api/", "gui/", "dyncx/"],
        "test_paths": ["tests/"],
        "ci_paths": [".github/"],
        "tooling_paths": ["maintenance/"],
        "docs_paths": [],
        "generated_paths": [],
    })

    _run_subprocess([
        sys.executable, str(SCRIPTS_DIR / "derive_repo_profile.py"),
        "--run", str(run_dir),
    ])

    profile = json.loads((run_dir / "repo_profile.json").read_text())

    assert profile["source"] == "derived"
    assert "python" in profile["languages"]
    assert "django" in profile["framework_hints"]
    # Django entrypoint patterns surfaced.
    assert "**/views.py" in profile["entrypoint_paths"]
    assert "**/urls.py" in profile["entrypoint_paths"]
    # hot/cold prefixes preserve the analysis output's intent.
    assert set(profile["hot_path_prefixes"]) == {"api/", "gui/", "dyncx/"}
    assert "tests/" in profile["cold_path_prefixes"]
    assert ".github/" in profile["cold_path_prefixes"]
    assert "maintenance/" in profile["cold_path_prefixes"]
    # Bootstrap fields are gone — we replaced "bootstrap" with "derived".
    assert profile["notes"]


def test_derive_excludes_excluded_files_from_languages(tmp_path):
    """A repo with one .py file and many excluded binary files should report
    python only — not the binary 'extensions' (which are usually empty)."""
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    repo_root.mkdir()
    (repo_root / "app.py").write_text("x = 1\n")
    _init_run(repo_root, run_dir)
    _seed_files(run_dir, [
        ("app.py", ".py", False),
        ("img1.png", ".png", True),
        ("img2.png", ".png", True),
        ("img3.png", ".png", True),
    ])
    _write_analysis(run_dir, {"schema_version": 1, "production_paths": []})

    _run_subprocess([
        sys.executable, str(SCRIPTS_DIR / "derive_repo_profile.py"),
        "--run", str(run_dir),
    ])
    profile = json.loads((run_dir / "repo_profile.json").read_text())
    assert profile["languages"] == ["python"]


def test_derive_handles_unknown_extension_via_extension_fallback(tmp_path):
    """Extensions not in _EXTENSION_LANGUAGE fall back to the bare extension
    (e.g., '.kt' missing → 'kt'). Sanity check we don't drop them entirely."""
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    repo_root.mkdir()
    (repo_root / "x.zig").write_text("// zig\n")
    _init_run(repo_root, run_dir)
    _seed_files(run_dir, [("x.zig", ".zig", False)])
    _write_analysis(run_dir, {"schema_version": 1})

    _run_subprocess([
        sys.executable, str(SCRIPTS_DIR / "derive_repo_profile.py"),
        "--run", str(run_dir),
    ])
    profile = json.loads((run_dir / "repo_profile.json").read_text())
    assert "zig" in profile["languages"]


# ---- depfile-based framework detection ----

@pytest.mark.parametrize("depfile,content,expected_label", [
    ("requirements.txt", "Flask==2.3.0\n", "flask"),
    ("requirements.txt", "fastapi>=0.110\nuvicorn\n", "fastapi"),
    ("pyproject.toml", '[tool.poetry.dependencies]\nflask = "^3"\n', "flask"),
    ("pyproject.toml", '[project]\ndependencies = ["fastapi"]\n', "fastapi"),
    ("package.json", '{ "dependencies": { "express": "^4.18" } }', "express"),
    ("package.json", '{ "dependencies": { "@nestjs/core": "^10" } }', "nestjs"),
    ("package.json", '{ "dependencies": { "fastify": "^4" } }', "fastify"),
    ("Gemfile", "gem 'sinatra'\n", "sinatra"),
    ("composer.json", '{ "require": { "symfony/framework-bundle": "^6" } }', "symfony"),
    ("pom.xml", "<artifactId>spring-boot-starter-web</artifactId>", "spring"),
    ("go.mod", "require github.com/gin-gonic/gin v1.9.1\n", "gin"),
    ("go.mod", "require github.com/labstack/echo/v4 v4.11.0\n", "echo"),
    ("Cargo.toml", '[dependencies]\naxum = "0.7"\n', "axum"),
    ("Cargo.toml", '[dependencies]\nactix-web = "4"\n', "actix-web"),
])
def test_depfile_framework_detection(tmp_path, depfile, content, expected_label):
    """Each depfile/content combination produces the expected framework hint."""
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    repo_root.mkdir(parents=True)
    (repo_root / depfile).write_text(content)
    _init_run(repo_root, run_dir)
    _write_analysis(run_dir, {"schema_version": 1})

    _run_subprocess([
        sys.executable, str(SCRIPTS_DIR / "derive_repo_profile.py"),
        "--run", str(run_dir),
    ])
    profile = json.loads((run_dir / "repo_profile.json").read_text())
    assert expected_label in profile["framework_hints"], (
        f"expected {expected_label!r} from {depfile} content {content!r}, "
        f"got {profile['framework_hints']!r}"
    )


def test_depfile_framework_detection_negative(tmp_path):
    """A pyproject without web framework deps should NOT emit flask/fastapi."""
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    repo_root.mkdir(parents=True)
    (repo_root / "pyproject.toml").write_text(
        '[project]\ndependencies = ["click", "rich"]\n'
    )
    _init_run(repo_root, run_dir)
    _write_analysis(run_dir, {"schema_version": 1})

    _run_subprocess([
        sys.executable, str(SCRIPTS_DIR / "derive_repo_profile.py"),
        "--run", str(run_dir),
    ])
    profile = json.loads((run_dir / "repo_profile.json").read_text())
    assert "flask" not in profile["framework_hints"]
    assert "fastapi" not in profile["framework_hints"]
