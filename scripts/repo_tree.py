"""Render a repository tree for the analyze_repo prompt."""
from __future__ import annotations

import re
from pathlib import Path


def build_repo_tree(repo_root: Path, max_depth: int = 8) -> str:
    """Render the full repository tree for analyze_repo.

    Renders every directory and every distinctive file. Skips known noise
    (`node_modules/`, `.git/`, `dist/`, `build/`, etc.) with a `(skipped)`
    marker. Hidden entries are dropped except `.github/` and
    `.devcontainer/`. Build/manifest files (`package.json`, `Cargo.toml`,
    `Dockerfile`, …) are marked with ` (*)`.

    Per-directory file rendering is signal-preserving but compresses
    repetitive runs:

      * Subdirectories: always rendered, recursed up to ``max_depth``.
      * Distinctive files — build manifests, vendored libraries
        (``*.min.js``, ``leaflet@1.2.0.js``, ``bootstrap-4.5.1.css``,
        ``jquery*``, ``lodash*``, …), top-level docs (README, LICENSE,
        ``*.md``, ``*.rst``) — always rendered individually so vendored
        detection signal survives.
      * Other files are grouped by extension. A group of ≥4 files of the
        same extension renders as ``[N .ext files: a, b, c, +K more]``;
        smaller groups render individually.

    Why no entry budget: real first-party repos after SKIP_DIRS pruning
    sit at 1–5K entries (~10–15K tokens) and fit comfortably in the 64K
    analyze_repo context alongside README and task prompt. Extension
    grouping bounds worst-case inflation from blob-y directories
    (``static/<thousand_images>/``).
    """
    SKIP_DIRS = {
        "node_modules", ".git", "dist", "target", "build",
        ".next", "__pycache__", "vendor", ".venv", "venv",
        ".gradle", ".idea", ".vscode", "out",
    }
    BUILD_FILE_NAMES = {
        "Makefile", "CMakeLists.txt", "setup.py", "setup.cfg", "pyproject.toml",
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
        "build.gradle", "pom.xml", "meson.build", "configure.ac", "Dockerfile",
    }
    KNOWN_VENDORED_LIBS = {
        "jquery", "bootstrap", "leaflet", "highcharts", "echarts",
        "react", "vue", "lodash", "axios", "d3", "three",
        "moment", "underscore", "backbone", "angular", "ember",
        "datatables", "select2", "tinymce", "ckeditor", "chartjs",
    }
    DOC_BASES = {
        "README", "LICENSE", "CHANGELOG", "CONTRIBUTING", "AUTHORS",
        "NOTICE", "COPYING", "INSTALL", "SECURITY",
        "CODE_OF_CONDUCT", "MAINTAINERS",
    }
    GROUP_THRESHOLD = 4
    GROUP_SAMPLES = 3
    _VERSIONED_AT = re.compile(r"@\d+\.\d+")
    _VERSIONED_DASH = re.compile(r"-\d+\.\d+\.\d+")

    def _list_dir(d: Path) -> list[Path]:
        try:
            return sorted(
                d.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except (PermissionError, OSError):
            return []

    def _visible(entry: Path) -> bool:
        return not (
            entry.name.startswith(".")
            and entry.name not in (".github", ".devcontainer")
        )

    def _is_distinctive_file(name: str) -> bool:
        if name in BUILD_FILE_NAMES:
            return True
        # Docs (case-insensitive base match — "README", "Readme.md", "LICENSE")
        base_no_ext = name.split(".", 1)[0].upper()
        if base_no_ext in DOC_BASES:
            return True
        lower = name.lower()
        if lower.endswith((".md", ".rst")):
            return True
        # Vendored asset patterns
        if ".min." in lower:
            return True
        if _VERSIONED_AT.search(name) or _VERSIONED_DASH.search(name):
            return True
        # Known third-party library prefixes (jquery*, leaflet*, ...)
        first_token = re.split(r"[.\-_]", lower, maxsplit=1)[0]
        if first_token in KNOWN_VENDORED_LIBS:
            return True
        return False

    def _render(dir_path: Path, depth: int, prefix: str) -> list[str]:
        if depth > max_depth:
            return []
        out: list[str] = []
        children = [c for c in _list_dir(dir_path) if _visible(c)]
        dirs = [c for c in children if c.is_dir()]
        files = [c for c in children if not c.is_dir()]

        for d in dirs:
            if d.name in SKIP_DIRS:
                out.append(f"{prefix}{d.name}/  (skipped)")
                continue
            out.append(f"{prefix}{d.name}/")
            if depth + 1 <= max_depth:
                out.extend(_render(d, depth + 1, prefix + "  "))

        distinctive: list[Path] = []
        by_ext: dict[str, list[Path]] = {}
        for f in files:
            if _is_distinctive_file(f.name):
                distinctive.append(f)
                continue
            ext = f.suffix.lower() if f.suffix else "(no ext)"
            by_ext.setdefault(ext, []).append(f)

        for f in distinctive:
            mark = " (*)" if f.name in BUILD_FILE_NAMES else ""
            out.append(f"{prefix}{f.name}{mark}")

        for ext in sorted(by_ext.keys()):
            group = by_ext[ext]
            if len(group) < GROUP_THRESHOLD:
                for f in group:
                    out.append(f"{prefix}{f.name}")
            else:
                samples = [f.name for f in group[:GROUP_SAMPLES]]
                more = len(group) - GROUP_SAMPLES
                tail = f", +{more} more" if more > 0 else ""
                out.append(
                    f"{prefix}[{len(group)} {ext} files: "
                    f"{', '.join(samples)}{tail}]"
                )

        return out

    lines: list[str] = [f"{repo_root.name}/"]
    lines.extend(_render(repo_root, 1, "  "))

    return "\n".join(lines)
