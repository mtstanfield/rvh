#!/usr/bin/env python3
"""Classify a repo's paths by role (production/test/ci/docs/tooling/generated).

Runs once after inventory, writes `.rvh-runs/repo_analysis.json`, which is
read by the scorer to bias priority per-repo. Data model and response
parser first; the model call and CLI live at the bottom of this file.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class RepoAnalysis:
    """Per-repo classification of directories produced by the pre-scoring phase."""
    production_paths: list[str] = field(default_factory=list)
    test_paths: list[str] = field(default_factory=list)
    ci_paths: list[str] = field(default_factory=list)
    docs_paths: list[str] = field(default_factory=list)
    tooling_paths: list[str] = field(default_factory=list)
    generated_paths: list[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "production_paths": self.production_paths,
            "test_paths": self.test_paths,
            "ci_paths": self.ci_paths,
            "docs_paths": self.docs_paths,
            "tooling_paths": self.tooling_paths,
            "generated_paths": self.generated_paths,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RepoAnalysis":
        return cls(
            production_paths=list(data.get("production_paths", [])),
            test_paths=list(data.get("test_paths", [])),
            ci_paths=list(data.get("ci_paths", [])),
            docs_paths=list(data.get("docs_paths", [])),
            tooling_paths=list(data.get("tooling_paths", [])),
            generated_paths=list(data.get("generated_paths", [])),
            rationale=str(data.get("rationale", "")),
        )


import json as _json

_ANALYSIS_BLOCK_RE = re.compile(r"<analysis>(.*?)</analysis>", re.DOTALL)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_BARE_RE = re.compile(r"(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})", re.DOTALL)
_REQUIRED_JSON_FIELDS = {
    "production_paths", "test_paths", "ci_paths",
    "docs_paths", "tooling_paths", "generated_paths",
}


def _parse_json_analysis(text: str) -> RepoAnalysis | None:
    """Try to extract a RepoAnalysis from a JSON object in the model response.

    Looks for either a fenced ```json code block or a bare top-level JSON
    object whose keys include the expected analysis fields. Returns None if
    no parsable JSON object with the right shape is found.
    """
    candidates: list[str] = []
    fenced = _JSON_FENCE_RE.findall(text)
    candidates.extend(fenced)
    if not candidates:
        candidates.extend(_JSON_BARE_RE.findall(text))

    for blob in candidates:
        try:
            data = _json.loads(blob)
        except (ValueError, _json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if not (_REQUIRED_JSON_FIELDS & set(data.keys())):
            continue
        return RepoAnalysis.from_dict(data)
    return None


# Universal directory-name → bucket rules. The model's free-form judgment
# is authoritative for ambiguous paths, but for these names there is no
# legitimate disagreement: ``.github/`` is CI, ``node_modules/`` is
# generated, etc. We post-validate to catch trace-vs-output drift — the
# model sometimes reasons correctly in its trace ("`.github/` is CI →
# ci_paths") and then writes the JSON inconsistent with that reasoning
# (placing it in production_paths). Verified on coriolix 2026-05-03.
#
# Each entry maps a normalized path (with trailing slash) to its correct
# bucket. The validator moves any misclassified entry to its canonical
# bucket and emits a normalization event.
_CANONICAL_PATH_BUCKETS: dict[str, str] = {
    ".github/": "ci_paths",
    ".gitlab/": "ci_paths",
    ".circleci/": "ci_paths",
    ".buildkite/": "ci_paths",
    ".azure-pipelines/": "ci_paths",
    "node_modules/": "generated_paths",
    "vendor/": "generated_paths",
    "dist/": "generated_paths",
    "build/": "generated_paths",
    "target/": "generated_paths",
    "out/": "generated_paths",
    ".next/": "generated_paths",
    "__pycache__/": "generated_paths",
    "tests/": "test_paths",
    "test/": "test_paths",
    "spec/": "test_paths",
}


def normalize_repo_analysis(analysis: "RepoAnalysis") -> tuple["RepoAnalysis", list[str]]:
    """Apply universal directory-name rules and return (normalized, notes).

    Catches trace-vs-output drift where the model reasons correctly about a
    well-known directory but emits the JSON in the wrong bucket. Idempotent
    — running twice yields the same output and no new notes.

    Returns a fresh ``RepoAnalysis`` with corrections applied and a list of
    human-readable note strings describing each move (empty list when the
    input was already canonical).
    """
    bucket_attrs = (
        "production_paths", "test_paths", "ci_paths",
        "docs_paths", "tooling_paths", "generated_paths",
    )
    buckets: dict[str, list[str]] = {
        attr: list(getattr(analysis, attr)) for attr in bucket_attrs
    }
    notes: list[str] = []

    for canonical_path, target_bucket in _CANONICAL_PATH_BUCKETS.items():
        for src_bucket in bucket_attrs:
            if src_bucket == target_bucket:
                continue
            if canonical_path in buckets[src_bucket]:
                buckets[src_bucket].remove(canonical_path)
                if canonical_path not in buckets[target_bucket]:
                    buckets[target_bucket].append(canonical_path)
                notes.append(
                    f"moved {canonical_path} from {src_bucket} to {target_bucket} "
                    "(canonical bucket for this directory name)"
                )

    normalized = RepoAnalysis(
        production_paths=buckets["production_paths"],
        test_paths=buckets["test_paths"],
        ci_paths=buckets["ci_paths"],
        docs_paths=buckets["docs_paths"],
        tooling_paths=buckets["tooling_paths"],
        generated_paths=buckets["generated_paths"],
        rationale=analysis.rationale,
    )
    return normalized, notes


def parse_repo_analysis(text: str) -> RepoAnalysis | None:
    """Extract a RepoAnalysis from a model response.

    Accepts either a JSON object (what prompts/analyze_repo.md asks for) or
    an `<analysis>...</analysis>` XML block (an older format).
    Tolerates extra prose around the structured payload. Returns None if no
    parseable analysis is found.
    """
    if not text:
        return None

    # Prefer JSON — the newer prompt sets emit it natively.
    parsed = _parse_json_analysis(text)
    if parsed is not None:
        return parsed

    # Fall back to the legacy XML format.
    match = _ANALYSIS_BLOCK_RE.search(text)
    if match is None:
        return None
    xml_body = "<analysis>" + match.group(1) + "</analysis>"
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError:
        return None
    if root.tag != "analysis":
        return None

    def paths(section_name: str) -> list[str]:
        section = root.find(section_name)
        if section is None:
            return []
        return [p.text.strip() for p in section.findall("path") if p.text and p.text.strip()]

    rationale_elem = root.find("rationale")
    rationale = (rationale_elem.text or "").strip() if rationale_elem is not None else ""

    return RepoAnalysis(
        production_paths=paths("production_paths"),
        test_paths=paths("test_paths"),
        ci_paths=paths("ci_paths"),
        docs_paths=paths("docs_paths"),
        tooling_paths=paths("tooling_paths"),
        generated_paths=paths("generated_paths"),
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Model call + CLI
# ---------------------------------------------------------------------------

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import llm
from common import (
    append_event,
    connect_db,
    debug_log,
    load_repo_profile,
    require_run,
    save_repo_analysis,
    update_run_timestamp,
    utc_now,
)
from repo_tree import build_repo_tree


def _read_readme_snippet(repo_root: Path, max_bytes: int = 4096) -> str:
    """Return up to max_bytes of the repo-root README.md, or '' if absent/binary."""
    for candidate in ("README.md", "README", "Readme.md", "readme.md"):
        path = repo_root / candidate
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        try:
            text = data[:max_bytes].decode("utf-8")
        except UnicodeDecodeError:
            return ""
        return text
    return ""


def _language_summary_from_db(conn: sqlite3.Connection) -> dict:
    """Rebuild the language/extension histogram used by the scorer."""
    extensions: dict[str, int] = {}
    categories: dict[str, int] = {}
    for row in conn.execute("SELECT path, category FROM files WHERE excluded = 0"):
        ext = Path(row["path"]).suffix.lstrip(".").lower() or "noext"
        extensions[ext] = extensions.get(ext, 0) + 1
        if row["category"]:
            categories[row["category"]] = categories.get(row["category"], 0) + 1
    return {"extensions": extensions, "categories": categories}


def build_analysis_prompt(
    repo_tree: str,
    language_summary: dict,
    framework_hints: list[str],
    readme_snippet: str,
) -> str:
    """Build the prompt for repo classification."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from prompts.loader import load_prompt
    return load_prompt(
        "analyze_repo",
        repo_tree=repo_tree,
        language_extensions=json.dumps(language_summary.get("extensions", {})),
        language_categories=json.dumps(language_summary.get("categories", {})),
        framework_hints=json.dumps(framework_hints),
        readme_block=readme_snippet.strip() if readme_snippet else "none",
    )


def _ask(args, prompt: str) -> str:
    """One analyzer call against the run's endpoint/model ('' on failure)."""
    return llm.chat(
        prompt, endpoint=args.endpoint, model=args.model,
        api_key=os.environ.get("RVH_API_KEY"), timeout=900, label="analyze_repo",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify repo paths by role")
    parser.add_argument("--run", required=True, help="Run directory")
    parser.add_argument("--endpoint", required=True, help="OpenAI-compatible endpoint")
    parser.add_argument("--model", required=True, help="Model name")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    run_dir = Path(args.run).expanduser().resolve()

    with connect_db(run_dir) as conn:
        run = require_run(conn)
        repo_root = Path(run["repo_root"]).expanduser().resolve()
        language_summary = _language_summary_from_db(conn)

    profile = load_repo_profile(run_dir)
    framework_hints = list(profile.get("framework_hints", []))
    repo_tree = build_repo_tree(repo_root)
    readme_snippet = _read_readme_snippet(repo_root)

    prompt = build_analysis_prompt(repo_tree, language_summary, framework_hints, readme_snippet)

    if args.debug:
        debug_log(run_dir, "analyze_repo", "attempt1-input", prompt)

    response = _ask(args, prompt)
    if args.debug:
        debug_log(run_dir, "analyze_repo", "attempt1-output", response)

    analysis = parse_repo_analysis(response)

    if analysis is None:
        # One retry with a stricter reminder. Format-agnostic, matching the
        # parser's JSON and XML forms.
        retry_prompt = prompt + (
            "\n\nYour previous response did not match the required schema. "
            "Retry and emit ONLY the structured payload (the JSON object or "
            "XML block) exactly as specified above, with no additional prose."
        )
        if args.debug:
            debug_log(run_dir, "analyze_repo", "attempt2-input", retry_prompt)
        response = _ask(args, retry_prompt)
        if args.debug:
            debug_log(run_dir, "analyze_repo", "attempt2-output", response)
        analysis = parse_repo_analysis(response)

    if analysis is None:
        print("[analyze_repo] WARNING: analyzer produced malformed output on both tries", file=sys.stderr)
        analysis = RepoAnalysis(
            rationale="analyzer produced malformed output; scoring proceeds without layout guidance"
        )

    # Post-validate against canonical directory-name rules. The model
    # sometimes drifts between its reasoning trace and structured output
    # (e.g. trace says ".github/ is CI" but JSON puts .github/ in
    # production_paths). The normalization step is deterministic and
    # idempotent; it also catches mis-bucketed node_modules/dist/build/etc.
    analysis, normalization_notes = normalize_repo_analysis(analysis)
    if normalization_notes:
        for note in normalization_notes:
            print(f"[analyze_repo] normalization: {note}", file=sys.stderr)
        append_event(
            run_dir,
            {
                "event": "analyze_repo_normalized",
                "corrections": normalization_notes,
            },
        )

    save_repo_analysis(run_dir, analysis.to_dict())

    with connect_db(run_dir) as conn:
        run = require_run(conn)
        conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
            ("analyzed", utc_now(), run["id"]),
        )
        update_run_timestamp(conn, run["id"])

    print(json.dumps({"status": "analyzed", "path_groups": {
        "production": len(analysis.production_paths),
        "test": len(analysis.test_paths),
        "ci": len(analysis.ci_paths),
        "docs": len(analysis.docs_paths),
        "tooling": len(analysis.tooling_paths),
        "generated": len(analysis.generated_paths),
    }}), flush=True)


if __name__ == "__main__":
    main()
