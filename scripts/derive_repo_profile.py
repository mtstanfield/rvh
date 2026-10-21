#!/usr/bin/env python3
"""Derive repo_profile.json deterministically from repo_analysis.json + the
file inventory in state.db.

This runs after analyze_repo and before score_files in the bootstrap chain.
No LLM call — pure transform. The derived profile gives the scorer real
languages / framework_hints / hot_path_prefixes / cold_path_prefixes /
entrypoint_paths instead of the empty stub init_run wrote.

A user can still override anything by editing repo_profile.json
afterwards; this script merges into the existing profile so manual fields
are preserved (the merge is additive on list keys).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    append_event,
    connect_db,
    load_repo_analysis,
    load_repo_profile,
    merge_repo_profile,
    repo_profile_path,
    write_repo_profile,
)


# Top N extensions to surface as the repo's primary languages (extension
# count among non-excluded text files). Keep tight; this drives prompt-set
# selection downstream.
_TOP_LANGUAGES = 6


# Extension → language label. Anything not listed falls through to the
# raw extension (e.g., ".kt" → ".kt"). Curated to match prompt-set names.
_EXTENSION_LANGUAGE = {
    ".py": "python",
    ".pyi": "python",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".hh": "cpp",
    ".rs": "rust",
    ".cs": "csharp",
    ".swift": "swift",
    ".scala": "scala",
    ".html": "html",
    ".css": "css",
    ".sh": "bash",
    ".bash": "bash",
    ".sql": "sql",
}


# Marker file → framework label. Detected at repo root or inside any
# production_path. Keep this conservative — wrong hints poison scorer output.
_FRAMEWORK_MARKERS = {
    "manage.py": "django",
    "wsgi.py": "django",
    "asgi.py": "django",
    "settings.py": "django",
    "package.json": "node",
    "next.config.js": "nextjs",
    "next.config.mjs": "nextjs",
    "nuxt.config.ts": "nuxt",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
    "Gemfile": "ruby",
    "config/routes.rb": "rails",
    "composer.json": "php",
    "artisan": "laravel",
    "Dockerfile": "docker",
    "docker-compose.yml": "docker",
    "docker-compose.yaml": "docker",
    "Makefile": "make",
}


# Dependency-file regex → framework label. Used after marker-file detection
# to add web-framework hints that don't have a unique top-level marker. Each
# entry pairs a dep-file glob (relative to a search root) with substring
# patterns that must appear in the file content for the framework to be
# flagged. Patterns are intentionally narrow — the scorer's hot-path logic
# leans on these hints, so a wrong flag means wrong prioritization.
_DEPFILE_FRAMEWORK_PATTERNS: list[tuple[str, str, str]] = [
    # (dep_file, content_pattern, framework_label)
    # Python — pyproject.toml + requirements.txt are first-class; Pipfile rare.
    ("pyproject.toml", "flask",         "flask"),
    ("requirements.txt", "Flask",       "flask"),
    ("requirements.txt", "flask",       "flask"),
    ("Pipfile",          "flask",       "flask"),
    ("pyproject.toml",   "fastapi",     "fastapi"),
    ("requirements.txt", "fastapi",     "fastapi"),
    ("Pipfile",          "fastapi",     "fastapi"),
    # JS/TS — package.json deps. We match on quoted package name to avoid
    # matching script descriptions or unrelated text.
    ("package.json", '"express"',          "express"),
    ("package.json", '"@nestjs/core"',     "nestjs"),
    ("package.json", '"fastify"',          "fastify"),
    ("package.json", '"koa"',              "koa"),
    ("package.json", '"@hapi/hapi"',       "hapi"),
    # Ruby — Gemfile lines like `gem 'sinatra'`.
    ("Gemfile", "sinatra",  "sinatra"),
    # PHP — composer.json deps, quoted package name.
    ("composer.json", '"symfony/',                  "symfony"),
    ("composer.json", '"slim/slim"',                "slim"),
    # Java — Spring marker is unmistakable in pom.xml or build.gradle.
    ("pom.xml",          "spring-boot-starter",  "spring"),
    ("pom.xml",          "spring-webmvc",        "spring"),
    ("build.gradle",     "spring-boot-starter",  "spring"),
    ("build.gradle.kts", "spring-boot-starter",  "spring"),
    # Go — go.mod imports. Standard library net/http isn't listed there.
    ("go.mod", "github.com/gin-gonic/gin",   "gin"),
    ("go.mod", "github.com/labstack/echo",   "echo"),
    ("go.mod", "github.com/go-chi/chi",      "chi"),
    ("go.mod", "github.com/gofiber/fiber",   "fiber"),
    # Rust — Cargo.toml deps.
    ("Cargo.toml", 'axum =',          "axum"),
    ("Cargo.toml", 'axum.version',    "axum"),
    ("Cargo.toml", 'actix-web',       "actix-web"),
    ("Cargo.toml", 'rocket =',        "rocket"),
    ("Cargo.toml", 'rocket.version',  "rocket"),
    ("Cargo.toml", 'warp =',          "warp"),
]


def _scan_depfile_frameworks(search_roots: list[Path]) -> set[str]:
    """Read dep files at each search root and return matched framework labels.

    Failures (missing file, unreadable, non-utf8) are silently skipped — this
    is a hint generator, not a validator. False positives are worse than false
    negatives because the abstract gate-audit pattern in the recipes covers
    undetected frameworks reasonably well.
    """
    found: set[str] = set()
    for root in search_roots:
        for depfile, pattern, label in _DEPFILE_FRAMEWORK_PATTERNS:
            path = root / depfile
            if not path.is_file():
                continue
            try:
                # Cap the read at 256K — dep files larger than that are
                # almost always pathological (vendored lockfiles, etc.) and
                # not worth scanning.
                text = path.read_text(encoding="utf-8", errors="ignore")[:262144]
            except OSError:
                continue
            if pattern in text:
                found.add(label)
    return found


# Framework → entrypoint glob list. Only emit globs whose framework was
# actually detected, so the scorer doesn't get noise.
_FRAMEWORK_ENTRYPOINTS = {
    "django": ["**/views.py", "**/urls.py", "**/admin.py", "**/api/**/*.py"],
    "node": ["**/server.js", "**/index.js", "**/app.js", "**/routes/**/*.js"],
    "nextjs": ["pages/api/**/*.{js,ts}", "app/api/**/*.{js,ts}"],
    "go": ["**/main.go", "**/cmd/**/*.go"],
    "rust": ["**/main.rs", "**/bin/**/*.rs"],
    "maven": ["**/Main.java", "**/Application.java", "**/*Controller.java"],
    "gradle": ["**/Main.java", "**/Application.java", "**/*Controller.java"],
    "rails": ["app/controllers/**/*.rb", "config/routes.rb"],
    "laravel": ["app/Http/Controllers/**/*.php", "routes/**/*.php"],
    "php": ["**/index.php"],
}


def _languages_from_files(conn) -> list[str]:
    """Return the top language labels by file count among non-excluded text files."""
    rows = conn.execute(
        "SELECT extension, COUNT(*) AS n FROM files "
        "WHERE excluded = 0 AND is_binary = 0 "
        "GROUP BY extension ORDER BY n DESC"
    ).fetchall()
    seen: list[str] = []
    for row in rows:
        ext = (row["extension"] or "").lower()
        if not ext:
            continue
        label = _EXTENSION_LANGUAGE.get(ext, ext.lstrip("."))
        if label and label not in seen:
            seen.append(label)
        if len(seen) >= _TOP_LANGUAGES:
            break
    return seen


def _framework_hints(repo_root: Path, production_paths: list[str]) -> list[str]:
    """Detect frameworks via marker files at repo root or in production paths."""
    hints: set[str] = set()
    search_roots = [repo_root]
    for prod in production_paths:
        candidate = (repo_root / prod).resolve()
        if candidate.exists() and candidate.is_relative_to(repo_root):
            search_roots.append(candidate)
    for root in search_roots:
        for marker, framework in _FRAMEWORK_MARKERS.items():
            # Marker can be a single filename or a relative path like
            # config/routes.rb; treat both uniformly.
            if (root / marker).exists():
                hints.add(framework)
    hints |= _scan_depfile_frameworks(search_roots)
    return sorted(hints)


def _entrypoint_paths(framework_hints: list[str]) -> list[str]:
    """Union the entrypoint globs for each detected framework."""
    paths: set[str] = set()
    for fw in framework_hints:
        paths.update(_FRAMEWORK_ENTRYPOINTS.get(fw, []))
    return sorted(paths)


def _normalize_prefix(p: str) -> str:
    """Profile prefixes are repo-relative directories with trailing slash."""
    p = p.strip().strip("/")
    return f"{p}/" if p else ""


def derive_profile(run_dir: Path, repo_root: Path) -> dict:
    """Compute the derived profile dict (does not write to disk)."""
    analysis = load_repo_analysis(run_dir) or {}

    production = list(analysis.get("production_paths", []))
    cold_sources = (
        list(analysis.get("test_paths", []))
        + list(analysis.get("ci_paths", []))
        + list(analysis.get("docs_paths", []))
        + list(analysis.get("generated_paths", []))
        + list(analysis.get("tooling_paths", []))
    )

    hot_prefixes = sorted({_normalize_prefix(p) for p in production if p})
    cold_prefixes = sorted({_normalize_prefix(p) for p in cold_sources if p})
    # Don't double-count: a path classified as both production AND
    # tooling (rare but possible) should win as hot.
    cold_prefixes = [p for p in cold_prefixes if p not in hot_prefixes]

    framework_hints = _framework_hints(repo_root, production)
    entrypoint_paths = _entrypoint_paths(framework_hints)

    with connect_db(run_dir) as conn:
        languages = _languages_from_files(conn)

    return {
        "languages": languages,
        "framework_hints": framework_hints,
        "entrypoint_paths": entrypoint_paths,
        "hot_path_prefixes": hot_prefixes,
        "hot_paths": [],
        "cold_path_prefixes": cold_prefixes,
        "cold_paths": [],
        "keyword_boosts": {},
        "notes": "Derived from repo_analysis.json + file inventory.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Path to the .rvh-runs directory")
    parser.add_argument(
        "--repo", default=None,
        help="Repo root (defaults to config.repo_root from run_dir/config.json)",
    )
    args = parser.parse_args()

    run_dir = Path(args.run).expanduser().resolve()
    if args.repo:
        repo_root = Path(args.repo).expanduser().resolve()
    else:
        from common import read_config
        repo_root = Path(read_config(run_dir)["repo_root"])

    derived = derive_profile(run_dir, repo_root)
    existing = load_repo_profile(run_dir)
    merged = merge_repo_profile(existing, derived, source_override="derived")
    write_repo_profile(run_dir, merged)

    append_event(
        run_dir,
        {
            "event": "repo_profile_derived",
            "profile_path": str(repo_profile_path(run_dir)),
            "languages": merged.get("languages", []),
            "framework_hints": merged.get("framework_hints", []),
            "hot_prefix_count": len(merged.get("hot_path_prefixes", [])),
            "cold_prefix_count": len(merged.get("cold_path_prefixes", [])),
        },
    )

    import json
    print(json.dumps(merged, indent=2))


if __name__ == "__main__":
    main()
