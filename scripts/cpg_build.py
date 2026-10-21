"""CPG build orchestration for rvh.

Owns source-tree hashing, rebuild detection, and `joern-parse` invocation.
Each build writes `metadata.json` next to the CPG (lang, source_hash,
joern_version, built_at, build_duration_ms); a rebuild happens when the
source hash or the joern image changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# Pinned by digest (was :nightly). Bump deliberately: a new digest changes
# get_joern_version(), which invalidates every cached CPG.
JOERN_IMAGE = "ghcr.io/joernio/joern@sha256:869aa86c27609d78d1ca4a48b88304bcfd7681f85f4f7ac6557a7aff16a2960d"


def cpg_jvm_docker_args() -> list[str]:
    """`docker run` args capping the joern JVM heap. joern's default heap OOMs on
    large CPGs (e.g. a 95 MB C++ graph), which wedges both the build and the
    sidecar. ``JAVA_TOOL_OPTIONS`` is honored by the JVM itself, so a single env
    covers both joern-parse and joern --server. Size with ``RVH_CPG_HEAP_GB``
    (default 16); it must be <= the memory Docker Desktop is allotted
    (Settings → Resources → Memory)."""
    heap_gb = max(1, int(os.environ.get("RVH_CPG_HEAP_GB", "16")))
    return ["-e", f"JAVA_TOOL_OPTIONS=-Xmx{heap_gb}g"]


EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".pytest_cache",
    ".rvh-runs", ".rvh-cpg-cache",
    "vendor", "target", "build", ".venv", "venv",
})


def hash_source_tree(root: Path) -> str:
    """Compute a deterministic sha256 hash of the source tree.

    Excludes version-control dirs, build output, and run state. Sorted walk
    for order independence. File content + relative path both contribute.
    """
    h = hashlib.sha256()
    root = Path(root).resolve()
    paths = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.is_symlink():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        paths.append((rel.as_posix(), p))
    paths.sort(key=lambda x: x[0])
    for rel, p in paths:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


@dataclass
class CpgMetadata:
    source_hash: str
    joern_version: str
    built_at: str  # ISO 8601
    build_duration_ms: int
    lang: str


def write_metadata(
    cpg_dir: Path,
    *,
    lang: str,
    source_hash: str,
    joern_version: str,
    build_duration_ms: int,
) -> None:
    """Write metadata.json alongside the CPG artifact."""
    cpg_dir = Path(cpg_dir)
    cpg_dir.mkdir(parents=True, exist_ok=True)
    meta = CpgMetadata(
        source_hash=source_hash,
        joern_version=joern_version,
        built_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        build_duration_ms=build_duration_ms,
        lang=lang,
    )
    (cpg_dir / "metadata.json").write_text(json.dumps(asdict(meta), indent=2))


def read_metadata(cpg_dir: Path) -> CpgMetadata | None:
    """Read metadata.json, return None on any error."""
    try:
        data = json.loads((Path(cpg_dir) / "metadata.json").read_text())
        return CpgMetadata(**data)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None


def should_rebuild(
    *,
    cpg_dir: Path,
    lang: str,
    source_hash: str,
    joern_version: str,
) -> bool:
    """Decide whether phase 4 must rebuild the CPG.

    Returns True when: artifact missing, metadata unreadable, source_hash
    differs, or joern_version differs.
    """
    cpg_dir = Path(cpg_dir)
    artifact = cpg_dir / f"{lang}.bin"
    if not artifact.exists():
        return True
    meta = read_metadata(cpg_dir)
    if meta is None:
        return True
    if meta.source_hash != source_hash:
        return True
    if meta.joern_version != joern_version:
        return True
    return False


class CpgBuildError(RuntimeError):
    """Raised when CPG build fails (docker/joern-parse nonzero, timeout, OOM, missing binary)."""


# File extension → CPG language key. Used by `detect_primary_language`
# to pick which Joern frontend to run on a mixed-content repo. Must match
# the keys in `LANG_TO_JOERN_ARG` below.
_EXT_TO_CPG_LANG: dict[str, str] = {
    # C / C++
    "c": "c", "h": "cpp",
    "cpp": "cpp", "cc": "cpp", "cxx": "cpp",
    "hpp": "cpp", "hxx": "cpp", "hh": "cpp",
    # Python
    "py": "python", "pyi": "python",
    # Go
    "go": "go",
    # Java
    "java": "java",
    # JavaScript / TypeScript (same frontend handles both, and Vue components)
    "js": "js", "jsx": "js", "mjs": "js", "cjs": "js", "vue": "js",
    "ts": "ts", "tsx": "ts",
    # PHP, Ruby, Kotlin, C#, Swift
    "php": "php",
    "rb": "ruby",
    "kt": "kotlin", "kts": "kotlin",
    "cs": "csharp",
    "swift": "swift",
}


# Joern-frontend equivalence: jssrc indexes both .js and .ts in one CPG, and
# c2cpg indexes both .c and .cpp/.h. metadata.lang records whichever language
# key won the inventory tie-break, but the resulting CPG can answer queries
# for any extension that maps to the same joern frontend.
_CPG_FRONTEND_GROUPS: dict[str, frozenset[str]] = {
    "c":      frozenset({"c", "cpp"}),
    "cpp":    frozenset({"c", "cpp"}),
    "js":     frozenset({"js", "ts"}),
    "ts":     frozenset({"js", "ts"}),
}


def anchor_supported_by_cpg(anchor_path: str, cpg_dir: Path) -> bool:
    """True iff the anchor's extension is covered by the CPG built in
    ``cpg_dir``. False on any of: missing metadata.json, anchor extension
    absent or unknown, anchor language not in the built CPG's frontend
    group. Callers use this to skip CPG MCP exposure for unsupported
    anchors (e.g. shell scripts in a Python-only CPG) so workers don't
    burn budget on `inspect()` calls that always return "no node"."""
    name = anchor_path.rsplit("/", 1)[-1] if "/" in anchor_path else anchor_path
    if "." not in name:
        return False
    ext = name.rsplit(".", 1)[-1].lower()
    anchor_lang = _EXT_TO_CPG_LANG.get(ext)
    if anchor_lang is None:
        return False
    meta = read_metadata(cpg_dir)
    if meta is None:
        return False
    group = _CPG_FRONTEND_GROUPS.get(meta.lang, frozenset({meta.lang}))
    return anchor_lang in group


def detect_primary_language(run_dir: Path, *, fallback: str = "cpp") -> str:
    """Pick a CPG language key from the inventory DB's top code extension.

    Reads files table, groups by extension for category='code', returns the
    CPG language key for the most common extension. Falls back to
    ``fallback`` when the inventory is empty, the DB is missing, or no
    known extension dominates.

    Ties go to whichever extension the DB returns first — the caller
    should treat polyglot repos as "pick one and go"; users can override
    via a CLI flag if they need a non-dominant language.
    """
    # Local import to avoid pulling common's deps at module import time
    # (cpg_build is also used standalone in tests with no DB setup).
    from common import connect_db  # noqa: PLC0415
    try:
        with connect_db(run_dir) as conn:
            rows = conn.execute(
                """SELECT LOWER(extension) AS ext, COUNT(*) AS cnt
                   FROM files
                   WHERE category = 'code' AND excluded = 0
                   GROUP BY ext ORDER BY cnt DESC"""
            ).fetchall()
    except Exception:
        return fallback
    for row in rows:
        lang = _EXT_TO_CPG_LANG.get((row["ext"] or "").lstrip("."))
        if lang:
            return lang
    return fallback


# Map our language key → joern-parse `--language` alias.
#
# Joern ships TWO aliases for many languages: a legacy bytecode/binary
# frontend (e.g. `python`, `javascript`, `java`) and a newer source-code
# frontend (e.g. `pythonsrc`, `jssrc`, `javasrc`). The legacy aliases are
# partly broken in the nightly image — `--language python` tries to invoke
# a `py2cpg.sh` script that doesn't exist. The source variants point at
# the frontend binaries that actually ship (`pysrc2cpg`, `jssrc2cpg`,
# `javasrc2cpg`).
#
# Prefer the source frontend everywhere; we analyse source trees, not
# binaries. Use `joern-parse --list-languages` inside the image to extend.
LANG_TO_JOERN_ARG = {
    "cpp":    ["--language", "c"],          # c2cpg.sh covers C/C++
    "c":      ["--language", "c"],
    "go":     ["--language", "golang"],     # gosrc2cpg
    "python": ["--language", "pythonsrc"],  # pysrc2cpg; 'python' alias = broken py2cpg.sh
    "js":     ["--language", "jssrc"],      # jssrc2cpg.sh
    "ts":     ["--language", "jssrc"],      # jssrc2cpg.sh (TS support included)
    "java":   ["--language", "javasrc"],    # javasrc2cpg; 'java' alias = jimple2cpg (bytecode)
    "php":    ["--language", "php"],        # php2cpg
    "ruby":   ["--language", "rubysrc"],    # rubysrc2cpg
    "kotlin": ["--language", "kotlin"],     # kotlin2cpg
    "csharp": ["--language", "csharpsrc"],  # csharpsrc2cpg
    "swift":  ["--language", "swiftsrc"],   # swiftsrc2cpg.sh
}


def get_joern_version() -> str:
    """Return an identifier for the Joern image — digest if pulled, tag otherwise.

    Used as the `joern_version` field in the CPG metadata sidecar so that
    pulling a new image (different digest) forces a rebuild. Does not invoke
    joern itself; joern-parse has no --version flag.

    Raises CpgBuildError if docker is missing entirely.
    """
    if shutil.which("docker") is None:
        raise CpgBuildError(
            "docker not found on PATH. See docs/cpg-host-setup.md"
        )
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", "--format={{.Id}}", JOERN_IMAGE],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError as e:
        raise CpgBuildError(f"docker not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise CpgBuildError(f"docker image inspect timed out: {e}") from e
    if r.returncode == 0 and r.stdout.strip():
        return f"{JOERN_IMAGE}@{r.stdout.strip()}"
    # image not present locally — run_joern_parse will pull on first use;
    # return the tag so we at least get rebuild-on-tag-change semantics
    return JOERN_IMAGE


def run_joern_parse(
    *, source_root: Path, output: Path, lang: str, timeout_sec: int
) -> None:
    """Invoke joern-parse inside a joernio/joern container.

    Source is bind-mounted read-only at /src, output dir writable at /out.
    """
    if lang not in LANG_TO_JOERN_ARG:
        raise CpgBuildError(f"unsupported language: {lang!r}")
    output.parent.mkdir(parents=True, exist_ok=True)
    src_abs = Path(source_root).resolve()
    out_dir_abs = output.parent.resolve()
    out_name = output.name
    cmd = [
        "docker", "run", "--rm", "--network", "none",
        *cpg_jvm_docker_args(),
        "-v", f"{src_abs}:/src:ro",
        "-v", f"{out_dir_abs}:/out",
        JOERN_IMAGE,
        "joern-parse",
        *LANG_TO_JOERN_ARG[lang],
        "--output", f"/out/{out_name}",
        "/src",
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec
        )
    except subprocess.TimeoutExpired as e:
        raise CpgBuildError(
            f"docker joern-parse timed out after {timeout_sec}s: {e}"
        ) from e
    if r.returncode != 0:
        raise CpgBuildError(
            f"docker joern-parse exited {r.returncode}: stderr={r.stderr[:1000]}"
        )


def build_cpg(
    *,
    source_root: Path,
    cpg_dir: Path,
    lang: str,
    timeout_sec: int = 1200,
) -> dict:
    """High-level: hash source, decide rebuild, invoke joern-parse if needed.

    Returns a dict summarizing the result:
      { "rebuilt": bool, "artifact": Path, "source_hash": str, "duration_ms": int }
    """
    joern_version = get_joern_version()
    source_hash = hash_source_tree(source_root)
    artifact = Path(cpg_dir) / f"{lang}.bin"
    if not should_rebuild(
        cpg_dir=cpg_dir, lang=lang,
        source_hash=source_hash, joern_version=joern_version,
    ):
        return {
            "rebuilt": False, "artifact": artifact,
            "source_hash": source_hash, "duration_ms": 0,
        }
    t0 = time.time()
    run_joern_parse(
        source_root=source_root, output=artifact, lang=lang, timeout_sec=timeout_sec
    )
    duration_ms = int((time.time() - t0) * 1000)
    write_metadata(
        cpg_dir, lang=lang, source_hash=source_hash,
        joern_version=joern_version, build_duration_ms=duration_ms,
    )
    return {
        "rebuilt": True, "artifact": artifact,
        "source_hash": source_hash, "duration_ms": duration_ms,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--source-root", required=True)
    p.add_argument("--cpg-dir", required=True)
    p.add_argument("--lang", required=True)
    p.add_argument("--timeout-sec", type=int, default=1200)
    args = p.parse_args()
    result = build_cpg(
        source_root=Path(args.source_root),
        cpg_dir=Path(args.cpg_dir),
        lang=args.lang,
        timeout_sec=args.timeout_sec,
    )
    print(json.dumps({
        "rebuilt": result["rebuilt"],
        "artifact": str(result["artifact"]),
        "source_hash": result["source_hash"],
        "duration_ms": result["duration_ms"],
    }, indent=2))
