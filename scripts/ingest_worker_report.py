"""Structured ingest of a worker's ``recipe__final_output`` into state.db."""

from __future__ import annotations

import json
from pathlib import Path

from common import append_event, connect_db, require_run, update_run_timestamp, utc_now
from report_utils import finding_fingerprint


def _ingest_structured(
    run_dir: Path, structured: dict, *, conn=None
) -> None:
    """Ingest a worker session's structured final_output dict.

    Inserts one row in ``worker_reports`` plus zero-or-more rows in
    ``findings`` — exactly one per finding for ``verdict == "finding"``,
    zero for ``verdict == "no-finding"``.

    ``conn`` is optional. When provided, the function reuses the
    caller's connection — important because the autonomous dispatcher
    already holds ``state.db`` open during ingest, and opening a second
    writer would deadlock on SQLite's serialized-writer lock. When
    ``conn`` is None (standalone tests, ad-hoc callers), a fresh
    connection is opened via ``connect_db``.

    Idempotent: same (task_id, fingerprint) tuple deduplicates via a
    pre-SELECT guard, matching the pattern used by ``worker._ingest_one_file``.
    """
    now = utc_now()
    task_id = int(structured["task_id"])
    verdict = str(structured["verdict"])
    anchor_file = str(structured["anchor_file"])
    summary = str(structured.get("summary") or "")
    dynamic_evidence = bool(structured.get("dynamic_evidence", False))
    findings = structured.get("findings") or []

    # Derive parent-level fields for the worker_reports row.
    if verdict == "no-finding" or not findings:
        parent_title = summary.split(". ")[0][:120] if summary else "no-finding"
        parent_severity = "info"
        parent_confidence = "low"
    else:
        n = len(findings)
        parent_title = f"{n} finding{'s' if n != 1 else ''} in {anchor_file.split('/')[-1]}"
        severity_rank = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        parent_severity = max(
            (f.get("severity", "info") for f in findings),
            key=lambda s: severity_rank.get(s, 0),
            default="medium",
        )
        confidences = [f.get("confidence") for f in findings if f.get("confidence")]
        conf_rank = {"low": 0, "medium": 1, "high": 2}
        parent_confidence = (
            max(confidences, key=lambda c: conf_rank.get(c, 0))
            if confidences
            else "medium"
        )

    if conn is not None:
        _do_structured_ingest(
            conn, run_dir, structured, now,
            task_id=task_id, verdict=verdict, anchor_file=anchor_file,
            findings=findings, dynamic_evidence=dynamic_evidence,
            parent_title=parent_title, parent_severity=parent_severity,
            parent_confidence=parent_confidence,
        )
    else:
        with connect_db(run_dir) as own_conn:
            _do_structured_ingest(
                own_conn, run_dir, structured, now,
                task_id=task_id, verdict=verdict, anchor_file=anchor_file,
                findings=findings, dynamic_evidence=dynamic_evidence,
                parent_title=parent_title, parent_severity=parent_severity,
                parent_confidence=parent_confidence,
            )

    append_event(
        run_dir,
        {
            "event": "worker_report_ingested",
            "task_id": task_id,
            "verdict": verdict,
            "title": parent_title,
        },
    )


def _do_structured_ingest(
    conn, run_dir: Path, structured: dict, now: str, *,
    task_id: int, verdict: str, anchor_file: str,
    findings: list, dynamic_evidence: bool,
    parent_title: str, parent_severity: str, parent_confidence: str,
) -> None:
    """Insert worker_reports row + per-finding rows on ``conn``.

    Extracted from ``_ingest_structured`` so the same body runs whether
    the caller passed its own connection or we opened one ourselves —
    avoids the dual-writer SQLite deadlock observed when the autonomous
    dispatcher's connection overlapped with our own ``connect_db`` open.
    """
    run = require_run(conn)
    report_id = conn.execute(
        """
        INSERT INTO worker_reports
        (task_id, report_path, title, verdict, severity, confidence,
         metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            str(run_dir / "reports" / f"task-{task_id}.md"),
            parent_title,
            verdict,
            parent_severity,
            parent_confidence,
            json.dumps(structured, sort_keys=True),
            now,
        ),
    ).lastrowid

    for finding in findings:
        title = str(finding["title"])
        finding_type = str(finding["finding_type"])
        severity = str(finding["severity"])
        confidence = str(finding.get("confidence") or "medium")
        cwe = finding.get("cwe") or None  # "" → NULL
        sink_location = str(finding["sink_location"])
        why_insufficient = str(finding.get("why_insufficient") or "")
        finding_summary = str(finding.get("summary") or why_insufficient)
        fp = finding_fingerprint(title, finding_type, anchor_file, [sink_location])

        # Idempotency: skip if a row with this (task_id, fingerprint) already exists.
        existing = conn.execute(
            "SELECT id FROM findings WHERE task_id = ? AND fingerprint = ?",
            (task_id, fp),
        ).fetchone()
        if existing:
            continue

        finding_status = "candidate"

        conn.execute(
            """
            INSERT INTO findings
            (task_id, report_id, title, finding_type, severity, confidence,
             status, anchor_file, related_files_json, fingerprint, summary,
             dynamic_evidence, cwe, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                report_id,
                title,
                finding_type,
                severity,
                confidence,
                finding_status,
                anchor_file,
                json.dumps([sink_location]),
                fp,
                finding_summary,
                1 if dynamic_evidence else 0,
                cwe,
                now,
            ),
        )

    update_run_timestamp(conn, run["id"])

