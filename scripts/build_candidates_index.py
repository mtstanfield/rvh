#!/usr/bin/env python3
"""Build a run-level candidate index (CANDIDATES.md) from worker reports.

Aggregates every `reports/task-*.md` report's `# Summary` section -- the
worker's complete candidate write-up -- into one severity-ordered file an
agent reviewer can triage from. Sourced from the prose summary, NOT
state.db, so it captures every candidate the worker wrote even when the
structured `findings[]` array under-counts (dense models reliably put the
full list in the summary; `findings[]` is best-effort metadata).

Usage:
    python3 scripts/build_candidates_index.py --run-dir <repo>/.rvh-runs
    # writes <run-dir>/CANDIDATES.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_utils import load_report  # noqa: E402

# Triage order: most severe first.
SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]


def summary_section(body: str) -> str:
    """Return the report's `# Summary` section with line structure preserved.

    Unlike report_utils.extract_summary (which flattens to a single line for
    fingerprinting), this keeps newlines so the worker's per-candidate
    blocks render as written.
    """
    out: list[str] = []
    in_summary = False
    for line in body.splitlines():
        if line.startswith("# "):
            in_summary = line.strip().lower() == "# summary"
            continue
        if in_summary:
            out.append(line)
    return "\n".join(out).strip()


def collect_candidates(reports_dir: Path) -> list[dict]:
    """Parse every `task-*.md` report with a finding verdict into candidate
    dicts. Skips no-finding reports and any report that fails to parse (logged
    to stderr -- one malformed report must not kill the index)."""
    out: list[dict] = []
    for report in sorted(reports_dir.glob("task-*.md")):
        try:
            meta, body = load_report(report)
        except Exception as exc:  # noqa: BLE001
            print(f"[candidates-index] skip {report.name}: {exc}", file=sys.stderr)
            continue
        if str(meta.get("verdict")) == "no-finding":
            continue
        out.append({
            "severity": str(meta.get("severity", "info")),
            "anchor_file": str(meta.get("anchor_file", "?")),
            "task_id": meta.get("task_id", "?"),
            "title": str(meta.get("title", "")),
            "summary": summary_section(body),
        })
    return out


def render_index(candidates: list[dict]) -> str:
    """Render the candidate list as severity-grouped markdown."""
    n_files = len({c["anchor_file"] for c in candidates})
    lines = [
        "# Candidate findings",
        "",
        f"{len(candidates)} report(s) with candidates across {n_files} file(s). "
        "Generated from worker report summaries (the complete candidate list — "
        "`findings[]`/state.db are best-effort metadata). Drill into the per-file "
        "`reports/task-N.md` for detail and validate against the code.",
        "",
    ]
    by_sev: dict[str, list[dict]] = {s: [] for s in SEVERITY_ORDER}
    for c in candidates:
        by_sev.setdefault(c["severity"], by_sev.setdefault("info", [])).append(c)
    for sev in SEVERITY_ORDER:
        group = sorted(by_sev.get(sev, []), key=lambda c: str(c["anchor_file"]))
        if not group:
            continue
        lines.append(f"## {sev}")
        lines.append("")
        for c in group:
            lines.append(f"### {c['anchor_file']}  ·  task {c['task_id']}")
            lines.append("")
            lines.append(c["summary"] if c["summary"] else f"_{c['title']}_")
            lines.append("")
            lines.append("---")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_index(run_dir: Path) -> tuple[Path, int]:
    """Write `<run_dir>/CANDIDATES.md` from `<run_dir>/reports/`.

    Returns (output_path, candidate_count). Safe when reports/ is absent
    (writes a header-only index with zero candidates).
    """
    reports_dir = run_dir / "reports"
    candidates = collect_candidates(reports_dir) if reports_dir.is_dir() else []
    out_path = run_dir / "CANDIDATES.md"
    out_path.write_text(render_index(candidates), encoding="utf-8")
    return out_path, len(candidates)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build CANDIDATES.md from worker reports.")
    ap.add_argument(
        "--run-dir", default=".rvh-runs",
        help="Run directory containing reports/ (default: .rvh-runs)",
    )
    args = ap.parse_args()
    out_path, n = build_index(Path(args.run_dir))
    print(f"[candidates-index] wrote {out_path} ({n} candidate report(s))")


if __name__ == "__main__":
    main()
