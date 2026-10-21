#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import re
from pathlib import Path


REQUIRED_KEYS = {"verdict", "finding_type", "severity", "title"}

OPTIONAL_KEYS = {
    "task_id",
    "anchor_file",
    "run_id",
    "related_files",
    "cwe",
    "notes",
    "dynamic_evidence",
    "confidence",
    # Where the sink is; useful pinning info when the model includes it.
    "sink_location",
    "sink_category",
    # Keys from earlier report formats, still accepted so their reports parse.
    "build_succeeded",
    "sanitizer_output",
    "validated",
    "defense_override_reason",
}

ALLOWED_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS

# Scratch-file paths an earlier report-writing recipe leaked into report
# frontmatter. They carry no signal, so they're dropped silently rather than
# rejected; any other unknown key still fails the strict-key check.
RECIPE_PARAM_NOISE_KEYS = frozenset({
    "defense_summary_path",
    "source_to_sink_excerpt_path",
})

ALLOWED_VERDICTS = {"finding", "no-finding"}

# Historical canonical bug classes. Kept as a SOFT hint for downstream
# filtering/grouping rather than a hard allowlist — real security bug
# classes far outgrow any fixed enum, and rejecting real findings like
# SSRF because they don't match the original 8 values is strictly bad
# for a defensive tool. Workers emit free-form strings; the optional
# `cwe` field gives structured classification when the model is
# confident enough to pick one.
CANONICAL_FINDING_TYPES = {
    "memory-safety",
    "auth-bypass",
    "injection",
    "race",
    "path-traversal",
    "logic-bug",
    "other",
    "none",  # valid when verdict is "no-finding"
}
ALLOWED_SEVERITIES = {"info", "low", "medium", "high", "critical"}
ALLOWED_CONFIDENCE = {"low", "medium", "high"}

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
QUOTED_STRING_RE = re.compile(r'^"(.*)"$')
_UNESCAPE_RE = re.compile(r"\\(.)")
CWE_RE = re.compile(r"^CWE-\d+$")

# Bare-scalar fallback for `key: value` lines where `value` was not quoted.
# Single-token values (no whitespace, commas, or YAML-structural chars) are
# accepted as strings: `severity: high`, `cwe: CWE-89`, `sink_location:
# gui/views.py:1968`, `anchor_file: /workspace/foo.py`. Multi-word values
# (`title: My finding`) remain rejected — the space is the canary that
# catches truncation/omitted-quote bugs the strict path was designed to flag.
# Downstream enum checks in validate_metadata still gate semantic validity
# (severity ∈ ALLOWED_SEVERITIES, cwe ∈ /^CWE-\d+$/), so the fallback only
# widens what's syntactically tolerable.
_BARE_SCALAR_RE = re.compile(r"^[^\s,\"'\[\]{}]+$")

# Severity blends like "medium-high" / "high/critical" appear when the model
# hedges between two enum values. Normalize to the higher of the two before
# the enum check — preserves the model's intent while staying compatible with
# the canonical 5-value severity ladder.
_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_SEVERITY_BLEND_SEP_RE = re.compile(r"\s*[-/]\s*")


# Extensions / filenames that legitimately trigger the step-0 non-code
# early-exit path in the worker prompt. Keep in sync with the extension
# list in the step-0 block in scripts/worker.py. When the anchor's extension
# / basename is NOT in this set, the worker is investigating a code file
# and must NOT emit the "Non-code anchor — out of scope" stub.
NONCODE_EXTS: frozenset[str] = frozenset({
    # Configuration / data
    ".conf", ".cnf", ".cfg", ".ini", ".properties",
    ".toml", ".yaml", ".yml", ".json", ".xml",
    ".csv", ".tsv", ".lock", ".sum",
    # Documentation
    ".md", ".rst", ".adoc", ".txt",
    # Build / packaging
    ".cmake", ".am", ".in", ".spec", ".dockerfile",
    # Web assets / styles
    ".html", ".css", ".svg",
})
NONCODE_BASENAMES: frozenset[str] = frozenset({"Dockerfile", "Makefile"})

# The stub title/summary prefix the step-0 block tells the worker to emit
# for non-code anchors. Downstream ingest uses these as the signal for
# "worker reached for the step-0 template" — if the anchor is actually a
# code file, that's a misclassification bug.
NONCODE_STUB_TITLE = "Non-code anchor — out of scope for this prompt"
NONCODE_STUB_SUMMARY_PREFIX = "Anchor file is a non-code file"


def is_noncode_anchor(anchor_file: str | None) -> bool:
    """True when the anchor path's extension / basename legitimately
    triggers the step-0 non-code early-exit.

    Empty / None → False (treat as code — safer default, because the
    check's consumers use a True result to SKIP guards).
    """
    if not anchor_file:
        return False
    from pathlib import Path as _Path
    p = _Path(anchor_file)
    if p.name in NONCODE_BASENAMES:
        return True
    return p.suffix.lower() in NONCODE_EXTS


def parse_scalar(value: str):
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value.isdigit():
        return int(value)
    match = QUOTED_STRING_RE.match(value)
    if match:
        # Reverse the escape applied by append_dynamic_validation's serializer:
        # \\" → " and \\\\ → \\. Uses a regex so the single scan handles all
        # \X sequences correctly (including nested backslash+quote combos).
        return _UNESCAPE_RE.sub(r"\1", match.group(1))
    if _BARE_SCALAR_RE.match(value):
        return value
    raise ValueError(
        "frontmatter values must be quoted strings, booleans, or integers; "
        f"got unsupported scalar {value!r}"
    )


def _normalize_severity(value: object) -> object:
    if not isinstance(value, str):
        return value
    if value in ALLOWED_SEVERITIES:
        return value
    parts = [p.strip().lower() for p in _SEVERITY_BLEND_SEP_RE.split(value.strip()) if p.strip()]
    if len(parts) >= 2 and all(p in ALLOWED_SEVERITIES for p in parts):
        return max(parts, key=_SEVERITY_RANK.__getitem__)
    return value


def parse_frontmatter(text: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("report is missing YAML-style frontmatter delimited by --- lines")

    frontmatter = match.group(1)
    body = text[match.end() :].strip()
    data: dict[str, object] = {}
    current_list_key: str | None = None

    for raw_line in frontmatter.splitlines():
        if not raw_line.strip():
            continue
        if raw_line.startswith("  - "):
            if current_list_key is None:
                raise ValueError(f"list item {raw_line!r} does not follow a list key")
            value = parse_scalar(raw_line[4:])
            if not isinstance(value, str):
                raise ValueError(f"list items must be quoted strings; got {raw_line!r}")
            data.setdefault(current_list_key, []).append(value)
            continue
        if raw_line.startswith(" "):
            raise ValueError(f"unsupported indentation in frontmatter line {raw_line!r}")
        if ":" not in raw_line:
            raise ValueError(f"frontmatter line missing ':' separator: {raw_line!r}")
        key, value = raw_line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in RECIPE_PARAM_NOISE_KEYS:
            current_list_key = None
            continue
        if key not in ALLOWED_KEYS:
            raise ValueError(f"unexpected frontmatter key {key!r}")
        if value == "":
            data[key] = []
            current_list_key = key
            continue
        data[key] = parse_scalar(value)
        current_list_key = None

    # Verdict inference: a report sometimes omits the `verdict` line. A
    # present, non-"none" finding_type pins the intent, so default rather
    # than reject; finding_type == "none" means "no-finding". When
    # finding_type is also absent, fall through to the missing-required
    # check below — that's a real malformed report.
    if "verdict" not in data:
        ft = data.get("finding_type", "")
        if isinstance(ft, str) and ft.strip():
            data["verdict"] = "no-finding" if ft.strip().lower() == "none" else "finding"

    missing = REQUIRED_KEYS - set(data)
    if missing:
        raise ValueError(f"report frontmatter missing required fields: {', '.join(sorted(missing))}")

    validate_metadata(data)
    return data, body


def _escape_yaml_str(s: str) -> str:
    """Escape a string for embedding inside a YAML double-quoted scalar."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_markdown_from_structured(structured: dict) -> str:
    """Render a human-readable markdown report from a structured worker
    output dict.

    The structured object is the source of truth; this function exists
    so reviewers can read findings on disk in the usual markdown shape
    without ingesting from the DB. The output's frontmatter parses
    cleanly via parse_frontmatter for round-trip compatibility with
    any tooling that still reads task-N.md.
    """
    verdict = structured.get("verdict", "no-finding")
    anchor_file = structured.get("anchor_file", "")
    task_id = int(structured.get("task_id", 0))
    summary = structured.get("summary", "")
    findings = structured.get("findings") or []
    dynamic_evidence = bool(structured.get("dynamic_evidence", False))

    # Build the parent-level fields. For finding-verdicts the top-level
    # cwe/severity reflect the worst finding so the ingest reader still
    # has a single-value view; per-finding details live in the body.
    if verdict == "finding" and findings:
        parent_severity = max(
            (f.get("severity", "info") for f in findings),
            key=lambda s: _SEVERITY_RANK.get(s, 0),
        )
        parent_cwe = findings[0].get("cwe", "")
        parent_finding_type = findings[0].get("finding_type", "unknown")
        parent_title = (
            f"{len(findings)} finding{'s' if len(findings) != 1 else ''} "
            f"in {anchor_file.split('/')[-1]}"
        )
    else:
        parent_severity = "info"
        parent_cwe = ""
        parent_finding_type = "none"
        parent_title = summary.split(". ")[0][:120] if summary else "no-finding"

    # Frontmatter — double-quote strings, bare booleans, integer task_id.
    fm_lines = [
        "---",
        f'verdict: "{verdict}"',
        f'finding_type: "{parent_finding_type}"',
        f'cwe: "{parent_cwe}"',
        f'severity: "{parent_severity}"',
        f'title: "{_escape_yaml_str(parent_title)}"',
        f"dynamic_evidence: {str(dynamic_evidence).lower()}",
        f'anchor_file: "{anchor_file}"',
        f"task_id: {task_id}",
        "---",
        "",
    ]

    body_lines = ["# Summary", "", summary, ""]

    if findings:
        body_lines.append("# Findings")
        body_lines.append("")
        for i, f in enumerate(findings):
            body_lines.append(
                f"## Finding {i}: {f.get('title', 'untitled')}"
            )
            body_lines.append("")
            body_lines.append(f"- **CWE**: {f.get('cwe', '')}")
            body_lines.append(f"- **Severity**: {f.get('severity', '')}")
            if f.get("confidence"):
                body_lines.append(f"- **Confidence**: {f['confidence']}")
            body_lines.append(f"- **Sink category**: {f.get('sink_category', '')}")
            body_lines.append(f"- **Sink location**: {f.get('sink_location', '')}")
            body_lines.append("")
            if f.get("summary"):
                body_lines.append(f["summary"])
                body_lines.append("")
            if f.get("source_to_sink"):
                body_lines.append("### Source → sink")
                body_lines.append("")
                body_lines.append(f["source_to_sink"])
                body_lines.append("")
            if f.get("why_insufficient"):
                body_lines.append("### Why defenses are insufficient")
                body_lines.append("")
                body_lines.append(f["why_insufficient"])
                body_lines.append("")
    else:
        body_lines.append("# Evidence")
        body_lines.append("")
        body_lines.append(
            f"Enumerated sinks across the anchor's reachable graph; "
            f"no real-boundary survivors."
        )
        body_lines.append("")

    return "\n".join(fm_lines + body_lines)


def validate_metadata(metadata: dict) -> None:
    missing = REQUIRED_KEYS - set(metadata.keys())
    if missing:
        raise ValueError(f"missing required keys: {missing}")

    unknown = set(metadata.keys()) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unknown keys: {unknown}")

    if metadata["verdict"] not in ALLOWED_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(ALLOWED_VERDICTS)}")
    # finding_type is free-form. It's the human-readable class name; the
    # optional `cwe` field (CWE-<id>) carries structured classification.
    # Still require non-empty string to catch truncation and typo bugs.
    ft = metadata["finding_type"]
    if not isinstance(ft, str) or not ft.strip():
        raise ValueError("finding_type must be a non-empty quoted string")
    metadata["severity"] = _normalize_severity(metadata["severity"])
    if metadata["severity"] not in ALLOWED_SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(ALLOWED_SEVERITIES)}")
    if not isinstance(metadata["title"], str) or not metadata["title"].strip():
        raise ValueError("title must be a non-empty quoted string")
    if "cwe" in metadata:
        cwe = metadata["cwe"]
        # Empty string is treated as "not set" (no CWE assigned). Workers
        # sometimes emit cwe: "" when they don't know or don't want to
        # commit to one; that's fine — same semantics as omitting the key.
        if not isinstance(cwe, str):
            raise ValueError(f"cwe must be a string; got {type(cwe).__name__}")
        if cwe and not CWE_RE.match(cwe):
            raise ValueError(f"cwe must match 'CWE-<digits>' (e.g. 'CWE-918') or be empty; got {cwe!r}")

    # Optional type checks — only when field present
    if "task_id" in metadata:
        if not isinstance(metadata["task_id"], int) or metadata["task_id"] <= 0:
            raise ValueError("task_id must be a positive integer")
    if "anchor_file" in metadata:
        if not isinstance(metadata["anchor_file"], str) or not metadata["anchor_file"]:
            raise ValueError("anchor_file must be a non-empty string")
    if "confidence" in metadata:
        if metadata["confidence"] not in ALLOWED_CONFIDENCE:
            raise ValueError(f"confidence must be one of {sorted(ALLOWED_CONFIDENCE)}")
    if "validated" in metadata:
        if metadata["validated"] not in (False, "false", True, "true"):
            raise ValueError("validated must be boolean-like")
    for opt_key in ("sink_location", "sink_category"):
        if opt_key in metadata:
            v = metadata[opt_key]
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"{opt_key} must be a non-empty string when present")
    related_files = metadata.get("related_files", [])
    if not isinstance(related_files, list) or any(not isinstance(item, str) or not item for item in related_files):
        raise ValueError("related_files must be a list of quoted non-empty strings")
    if "dynamic_evidence" in metadata:
        if metadata["dynamic_evidence"] not in (True, False, "true", "false"):
            raise ValueError("dynamic_evidence must be true or false")
    if "build_succeeded" in metadata:
        if metadata["build_succeeded"] not in (True, False, "true", "false"):
            raise ValueError("build_succeeded must be true or false")


def extract_summary(body: str) -> str:
    lines = [line.strip() for line in body.splitlines()]
    in_summary = False
    collected: list[str] = []

    for line in lines:
        if not line:
            if in_summary and collected:
                break
            continue
        if line.startswith("# "):
            in_summary = line.lower() == "# summary"
            continue
        if line.startswith("## "):
            if in_summary and collected:
                break
            continue
        if in_summary:
            collected.append(line)

    if collected:
        return " ".join(collected)

    for line in lines:
        if line and not line.startswith("#"):
            return line
    return ""


def normalize_report_text(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def finding_fingerprint(title: str, finding_type: str, anchor_file: str, related_files: list[str]) -> str:
    canonical = "|".join(
        [
            normalize_report_text(title),
            finding_type,
            anchor_file,
            ",".join(sorted(set(related_files))),
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_report(report_path: Path) -> tuple[dict, str]:
    text = report_path.read_text(encoding="utf-8")
    return parse_frontmatter(text)


