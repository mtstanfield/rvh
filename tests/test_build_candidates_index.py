"""Tests for build_candidates_index — the run-level CANDIDATES.md generator.

The index sources from the prose `# Summary` section (the worker's complete
candidate list), not state.db, and groups by severity. conftest.py puts
scripts/ on sys.path."""
from __future__ import annotations

from pathlib import Path

import build_candidates_index as idx


def _write_report(reports: Path, task_id: int, *, verdict: str, severity: str,
                  anchor: str, summary_body: str, finding_type: str = "authorization",
                  cwe: str = "CWE-862") -> None:
    fm = (
        "---\n"
        f'verdict: "{verdict}"\n'
        f'finding_type: "{finding_type}"\n'
        f'cwe: "{cwe}"\n'
        f'severity: "{severity}"\n'
        f'title: "{task_id} finding"\n'
        f'anchor_file: "{anchor}"\n'
        f"task_id: {task_id}\n"
        "---\n\n"
        "# Summary\n\n"
        f"{summary_body}\n"
    )
    (reports / f"task-{task_id}.md").write_text(fm, encoding="utf-8")


def test_summary_section_preserves_line_structure():
    body = "# Summary\n\n- A — high\n  - location: x.py:1\n\n# Findings\n\n## Finding 0\n"
    out = idx.summary_section(body)
    assert "- A — high" in out
    assert "  - location: x.py:1" in out  # multi-line block preserved
    assert "Finding 0" not in out  # stops at next top-level header


def test_collect_skips_no_finding_and_parses_findings(tmp_path: Path):
    reports = tmp_path / "reports"
    reports.mkdir()
    _write_report(reports, 18, verdict="finding", severity="high",
                  anchor="api/views.py", summary_body="- Missing auth — high\n  - location: api/views.py:295")
    _write_report(reports, 15, verdict="no-finding", severity="info",
                  anchor="api/permissions.py", summary_body="No vulnerabilities found.",
                  finding_type="none", cwe="")
    cands = idx.collect_candidates(reports)
    assert len(cands) == 1
    assert cands[0]["anchor_file"] == "api/views.py"
    assert "location: api/views.py:295" in cands[0]["summary"]


def test_render_orders_critical_before_high(tmp_path: Path):
    reports = tmp_path / "reports"
    reports.mkdir()
    _write_report(reports, 1, verdict="finding", severity="high",
                  anchor="b.py", summary_body="- high one")
    _write_report(reports, 2, verdict="finding", severity="critical",
                  anchor="a.py", summary_body="- crit one")
    text = idx.render_index(idx.collect_candidates(reports))
    assert text.index("## critical") < text.index("## high")
    assert "### a.py" in text and "### b.py" in text


def test_build_index_writes_file_and_counts(tmp_path: Path):
    reports = tmp_path / "reports"
    reports.mkdir()
    _write_report(reports, 1, verdict="finding", severity="critical",
                  anchor="a.py", summary_body="- crit")
    out_path, n = idx.build_index(tmp_path)
    assert out_path == tmp_path / "CANDIDATES.md"
    assert n == 1
    assert out_path.read_text().startswith("# Candidate findings")


def test_build_index_handles_missing_reports_dir(tmp_path: Path):
    out_path, n = idx.build_index(tmp_path)  # no reports/ subdir
    assert n == 0
    assert out_path.exists()
