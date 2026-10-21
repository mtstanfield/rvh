"""Reusable worker primitives for the autonomous runner."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from common import (
    DEFAULT_RUNS_DIR,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_NEEDS_FOLLOWUP,
    TASK_PENDING,
    TASK_REJECTED,
    TASK_REQUEUED,
    TASK_RESERVED,
    add_seconds,
    connect_db,
    debug_log,
    task_with_file,
    update_run_timestamp,
    utc_now,
)
from report_utils import (
    NONCODE_STUB_SUMMARY_PREFIX,
    NONCODE_STUB_TITLE,
    extract_summary,
    finding_fingerprint,
    is_noncode_anchor,
    load_report,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_OWNER = "autonomous-supervisor"
DEFAULT_STOP_FILE = ".stop-autonomous"

# Goose normally renders its own `system.md` for the system turn; it honors
# the `GOOSE_SYSTEM_PROMPT_FILE_PATH` env var and uses the file at that path
# verbatim instead. Each task dir is bind-mounted at /output in the
# container, so writing this file into the task dir makes it reachable as
# /output/system_prompt.md from inside.
GOOSE_SYSTEM_PROMPT_FILENAME = "system_prompt.md"

# Stated in every worker prompt: the sandbox has no network and no build, so
# the worker shouldn't spend turns on installs or compiles that can't work.
SANDBOX_ENVIRONMENT_BLOCK = (
    "\n\n## Environment\n\n"
    "You are in a sandbox with no network access and no compiled build of the target. "
    "Analyze statically with `rg`, `jq`, `git`, `python3` and, when available, the CPG "
    "tools; do not try to install packages or build the project.\n"
)
GOOSE_SYSTEM_PROMPT = (
    "You are an automated security analyst working through a "
    "vulnerability-review task. Detailed methodology is provided in the "
    "user turn. Use the available tools (shell, tree, write, edit, todo, "
    "and the CPG tools when the anchor's language has a CPG) to investigate "
    "the code and report through recipe__final_output.\n"
    "\n"
    "Operate with exhaustive coverage and concise deliberation: examine "
    "every handler and sink in your scope, but reason about each in one "
    "tight pass — name it, check its defense, conclude. Efficiency means "
    "cutting redundant deliberation (restating context, re-deriving a "
    "settled point, second-guessing a closed call), never investigating "
    "less. Prefer tool-backed evidence over prose reasoning.\n"
)


# ---------------------------------------------------------------------------
# Step-0 anchor-type gate — injected into worker.md via the {step0_block}
# placeholder. We emit the full non-code early-exit stub only when the
# anchor extension actually matches a non-code pattern; code anchors get a
# short replacement that tells the model step 0 does not apply. This
# removes the pull of the verbatim stub block for code-file investigations
# (observed 2026-04-21 on coriolix-3.0.1: workers with thinking enabled
# were reaching for the step-0 stub after a completed real investigation,
# clobbering valid no-finding reports).
# ---------------------------------------------------------------------------

# Non-code anchors get the early-exit block: report a no-finding stub through
# recipe__final_output on the first call instead of running the sink sweep.
_STEP0_NONCODE_BLOCK = """## Step 0 — Anchor-type gate (do this first)

Look at the anchor file path's extension. If it matches any of the patterns below, the anchor is a non-code file and falls outside this prompt's review scope. Do **NOT** start the investigation flow — do **NOT** run any grep / find / sweep commands. Report the no-finding stub immediately and end the session.

Patterns that trigger early exit:

- Configuration / data: `.conf`, `.cnf`, `.cfg`, `.ini`, `.properties`, `.toml`, `.yaml`, `.yml`, `.json`, `.xml`, `.csv`, `.tsv`, `.lock`, `.sum`
- Documentation: `.md`, `.rst`, `.adoc`, `.txt`
- Build / packaging: `.cmake`, `Makefile`, `.am`, `.in`, `.spec` (RPM spec), `.dockerfile`, `Dockerfile` (without extension)
- Web assets / styles: `.html`, `.css`, `.svg`

Why early exit: this prompt is built around code-bug analysis (memory safety, injection, deserialization, authorization-logic flaws, etc.) which doesn't apply to non-executable file types. A worker that runs the breadth-first sink sweep against a config anchor produces a runaway repo-wide grep with no useful signal and exhausts its time budget producing nothing. Misconfiguration findings (overly permissive defaults, exposed secrets, weak crypto choices) are real but require a different review pattern that this prompt does not implement.

**Early-exit report:** one `recipe__final_output` call with

- `verdict`: `"no-finding"`
- `anchor_file` and `task_id`: from the `## Task` block
- `findings`: `[]`
- `summary`: `"Anchor file is a non-code file (configuration, documentation, build manifest, or data). The current worker prompt is scoped to code-bug analysis (memory safety, injection, deserialization, authorization-logic flaws, etc.) and cannot review non-executable files. Misconfiguration / secrets-disclosure findings would require a different review prompt."`

Then end the session. Do NOT run grep / rg / find / view commands against the anchor or any other file. Do NOT enumerate sinks.

**Step 0 is a FIRST-CALL-ONLY gate.** The non-code early-exit stub above may only be your VERY FIRST tool call, and only when the file extension unambiguously matches the patterns listed. Once you have issued any other tool call — `todo_write`, `cat`, a CPG call (`entrypoints`, `xref`, `paths`, …), or a grep — the step-0 path is CLOSED for this session. You are committed to finishing the investigation flow above and producing a real defense-checked report. If mid-session you find yourself drawn to the non-code stub as a "quick exit", that is a wrong instinct: the file is a code file (you already started analyzing it), and the required output is a real no-finding (or finding) report with per-sink defense verdicts, not the out-of-scope stub.

If the anchor file is a code file (`.py`, `.c`, `.cpp`, `.cc`, `.cxx`, `.h`, `.hpp`, `.hh`, `.hxx`, `.go`, `.rs`, `.java`, `.kt`, `.js`, `.jsx`, `.ts`, `.tsx`, `.php`, `.rb`, `.pl`, `.cs`, `.swift`, `.scala`, `.sh`, `.bash`, or any other extension recognizable as source code in a programming language — **C/C++ header files count as code, review them the same as their implementation .cpp/.cc pairs**), follow the investigation flow above."""

# Code anchors get a short, unambiguous replacement that confirms step 0
# does not apply and names the stub by its exact title and summary so the
# model can recognize (and avoid) it later. Plain text, no placeholders.
_STEP0_CODE_SKIP_BLOCK = """## Step 0 — Anchor-type gate (does not apply here)

Anchor extension is `__ANCHOR_EXT__`. This is a code file, so the step-0 non-code early-exit does NOT apply to this task — follow the investigation flow above.

**Do NOT report the non-code stub** — a summary that begins "Anchor file is a non-code file", or a title "Non-code anchor — out of scope for this prompt". That stub is reserved for first-call early-exits on genuinely non-code anchors (`.yaml`, `.html`, `.md`, etc.). Using it after a code-file investigation is a reporting bug and will be rejected at ingest time, forcing the task to be re-run. When your defense check finds nothing, emit `recipe__final_output` with verdict `no-finding` and a summary that names each sink you checked and the defense that holds."""


def _build_step0_block(anchor_file: str) -> str:
    """Render the {step0_block} value for a worker prompt.

    Non-code extensions get the full step-0 early-exit block. Code extensions
    get a short skip block that names the step-0 stub so the model can
    recognize it and avoid reaching for it later in the session.
    """
    if is_noncode_anchor(anchor_file):
        return _STEP0_NONCODE_BLOCK
    ext = Path(anchor_file).suffix.lower() or "(none)"
    return _STEP0_CODE_SKIP_BLOCK.replace("__ANCHOR_EXT__", ext)


_REGION_BLOCK_TEMPLATE = """## Scope: lines __START__–__END__ of this file

Other regions of this file are separate tasks. You MAY read anywhere in the
repo for context (a sink in your region can be reached by a handler outside
it), but only report findings whose **sink line falls within
__START__–__END__**. This guarantees the whole file is covered without
double-reporting.
"""


def _build_region_block(payload: dict) -> str:
    """Render {region_block} when the task is scoped to a line range, else ''.

    Decomposed sub-tasks carry ``anchor_start_line``/``anchor_end_line``;
    whole-file tasks leave them NULL and get an empty block (unchanged
    behavior).
    """
    start = payload.get("anchor_start_line")
    end = payload.get("anchor_end_line")
    if not start or not end:
        return ""
    return (
        _REGION_BLOCK_TEMPLATE
        .replace("__START__", str(int(start)))
        .replace("__END__", str(int(end)))
    )


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def append_batch_journal(run_dir: Path, record: dict) -> None:
    path = run_dir / "logs" / "batches.jsonl"
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def extract_final_output(stdout_path: Path) -> dict | None:
    """Parse goose --output-format json stdout, return the last successful
    recipe__final_output tool-call args dict (or None if no call was made).

    Goose 1.35 prefixes the JSON dump with a session banner; we locate
    the JSON by finding the first '{\\n  "messages"' substring. When the
    session was SIGKILLed mid-write the trailing JSON is truncated;
    handle that by returning None rather than raising.

    When the model retried after a schema rejection, multiple
    final_output calls appear in the message history; return the last
    successful one (matches goose's internal FinalOutputTool state).
    """
    try:
        text = stdout_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None

    # Skip the goose banner — JSON dump starts at `{\n  "messages"` for
    # current goose. Fall back to `\n{\n` (brace at line start) in case
    # goose's banner formatting changes; a bare `{` fallback would match
    # any `{` inside the banner itself.
    idx = text.find('{\n  "messages"')
    if idx == -1:
        nl_idx = text.find('\n{\n')
        idx = nl_idx + 1 if nl_idx != -1 else -1
    if idx == -1:
        return None

    try:
        data = json.loads(text[idx:])
    except (json.JSONDecodeError, ValueError):
        return None

    last_args: dict | None = None
    for msg in data.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "toolRequest":
                continue
            tc = block.get("toolCall") or {}
            if tc.get("status") != "success":
                continue
            value = tc.get("value") or {}
            if value.get("name") == "recipe__final_output":
                args = value.get("arguments")
                if isinstance(args, dict):
                    last_args = args
    return last_args


# ---------------------------------------------------------------------------
# Cooldown management
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Logging / context helpers
# ---------------------------------------------------------------------------

def log_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Rate-limit detection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------

def reserve_exact_task(conn, task_id: int, owner: str, owner_session: str, lease_expires_at: str, max_attempts: int, now: str):
    row = conn.execute(
        """
        SELECT tasks.id AS task_id, tasks.priority, tasks.attempt_count, files.path,
               tasks.anchor_start_line, tasks.anchor_end_line
        FROM tasks
        JOIN files ON files.id = tasks.file_id
        WHERE tasks.id = ?
          AND tasks.status IN (?, ?)
        """,
        (task_id, TASK_PENDING, TASK_REQUEUED),
    ).fetchone()
    if row is None:
        return None
    next_attempt_count = int(row["attempt_count"] or 0)
    next_status = TASK_FAILED if next_attempt_count >= max_attempts else TASK_RESERVED
    conn.execute(
        """
        UPDATE tasks
        SET status = ?, owner = ?, owner_session = ?, reserved_at = ?, lease_expires_at = ?,
            heartbeat_at = NULL, attempt_count = ?, last_error = NULL, updated_at = ?
        WHERE id = ? AND status IN (?, ?)
        """,
        (
            next_status,
            owner,
            owner_session,
            now,
            lease_expires_at,
            next_attempt_count,
            now,
            task_id,
            TASK_PENDING,
            TASK_REQUEUED,
        ),
    )
    changed = conn.execute("SELECT changes() AS n").fetchone()["n"]
    if not changed or next_status != TASK_RESERVED:
        return None
    return {
        "task_id": row["task_id"],
        "priority": row["priority"],
        "path": row["path"],
        "anchor_start_line": row["anchor_start_line"],
        "anchor_end_line": row["anchor_end_line"],
        "owner": owner,
        "owner_session": owner_session,
        "lease_expires_at": lease_expires_at,
        "attempt_count": next_attempt_count,
    }


def start_task(conn, task_id: int, owner: str, owner_session: str, lease_expires_at: str, now: str) -> None:
    row = task_with_file(conn, task_id)
    if row is None:
        raise RuntimeError(f"task {task_id} not found")
    if row["owner"] != owner or row["owner_session"] != owner_session:
        raise RuntimeError(f"task {task_id} ownership mismatch")
    conn.execute(
        """
        UPDATE tasks
        SET status = ?, heartbeat_at = ?, lease_expires_at = ?, updated_at = ?,
            attempt_count = attempt_count + 1
        WHERE id = ? AND status = ?
        """,
        (TASK_IN_PROGRESS, now, lease_expires_at, now, task_id, TASK_RESERVED),
    )
    changed = conn.execute("SELECT changes() AS n").fetchone()["n"]
    if not changed:
        raise RuntimeError(f"task {task_id} failed to transition to in_progress")


def finalize_task(conn, task_id: int, owner: str, owner_session: str, status: str, notes: str, last_error: str | None, now: str) -> None:
    row = task_with_file(conn, task_id)
    if row is None:
        raise RuntimeError(f"task {task_id} not found")
    if row["owner"] != owner or row["owner_session"] != owner_session:
        raise RuntimeError(f"task {task_id} ownership mismatch during finalize")
    conn.execute(
        """
        UPDATE tasks
        SET status = ?, notes = ?, completed_at = ?, lease_expires_at = NULL, heartbeat_at = NULL,
            last_error = ?, updated_at = ?
        WHERE id = ? AND status IN (?, ?)
        """,
        (status, notes or None, now, last_error, now, task_id, TASK_RESERVED, TASK_IN_PROGRESS),
    )
    changed = conn.execute("SELECT changes() AS n").fetchone()["n"]
    if not changed:
        raise RuntimeError(f"task {task_id} failed to finalize")


def requeue_task_without_penalty(conn, task_id: int, owner: str, owner_session: str, notes: str, now: str) -> None:
    row = task_with_file(conn, task_id)
    if row is None:
        raise RuntimeError(f"task {task_id} not found")
    if row["owner"] != owner or row["owner_session"] != owner_session:
        raise RuntimeError(f"task {task_id} ownership mismatch during requeue")
    # Decrement attempt_count to reverse the increment that happened in start_task,
    # so the task can be retried without consuming an attempt slot.
    attempt_count = max(0, int(row["attempt_count"] or 0) - 1)
    conn.execute(
        """
        UPDATE tasks
        SET status = ?, owner = NULL, owner_session = NULL, notes = ?, reserved_at = NULL,
            lease_expires_at = NULL, heartbeat_at = NULL, completed_at = NULL,
            attempt_count = ?, last_error = ?, updated_at = ?
        WHERE id = ? AND status IN (?, ?)
        """,
        (TASK_REQUEUED, notes or None, attempt_count, notes or None, now, task_id, TASK_RESERVED, TASK_IN_PROGRESS),
    )
    changed = conn.execute("SELECT changes() AS n").fetchone()["n"]
    if not changed:
        raise RuntimeError(f"task {task_id} failed to requeue without penalty")


# ---------------------------------------------------------------------------
# Report ingestion
# ---------------------------------------------------------------------------

def _ingest_structured_via_worker(
    conn,
    run_id: str,
    report_path: Path,
    now: str,
    structured: dict,
    *,
    task_id: int | None = None,
    anchor_file: str | None = None,
    run_dir: Path | None = None,
) -> tuple[str, str, int | None]:
    """Worker-side adapter: call ingest_worker_report._ingest_structured,
    write the rendered markdown to disk, return the dispatcher view."""
    from ingest_worker_report import _ingest_structured  # noqa: PLC0415
    from report_utils import render_markdown_from_structured  # noqa: PLC0415

    if run_dir is None:
        raise ValueError("run_dir is required for structured ingest")

    # The same step-0 stub guard as the markdown path (see _ingest_one_file):
    # a code anchor's real investigation must not be replaced by the stub.
    effective_anchor_file = anchor_file or str(structured.get("anchor_file", ""))
    if (
        str(structured.get("summary", "")).lstrip().startswith(NONCODE_STUB_SUMMARY_PREFIX)
        and effective_anchor_file
        and not is_noncode_anchor(effective_anchor_file)
    ):
        raise ValueError(
            f"worker report uses the non-code early-exit stub but anchor "
            f"{effective_anchor_file!r} is a code file; requeue"
        )

    # Pass the caller's `conn` so we don't open a second SQLite writer —
    # the dispatcher holds state.db open across the whole ingest path, and
    # `connect_db` would block on the writer lock. Observed as "database is
    # locked" on task-15 of the 2026-05-26 smoke run.
    _ingest_structured(run_dir, structured, conn=conn)

    # Render markdown for human reviewers.
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_markdown_from_structured(structured), encoding="utf-8"
    )

    findings = structured.get("findings") or []
    verdict = str(structured.get("verdict", "no-finding"))
    if verdict == "finding" and findings:
        first = findings[0]
        fp = finding_fingerprint(
            str(first["title"]),
            str(first["finding_type"]),
            str(structured.get("anchor_file", "")),
            [str(first.get("sink_location", ""))],
        )
        row = conn.execute(
            "SELECT id FROM findings WHERE task_id = ? AND fingerprint = ? LIMIT 1",
            (int(structured["task_id"]), fp),
        ).fetchone()
        finding_id = row["id"] if row else None
        title = str(first["title"])
    else:
        finding_id = None
        summary = str(structured.get("summary", ""))
        title = summary.split(". ")[0][:120] if summary else "no-finding"

    return verdict, title, finding_id


def ingest_report(
    conn,
    run_id: str,
    report_path: Path,
    now: str,
    task_id: int | None = None,
    anchor_file: str | None = None,
    run_dir: Path | None = None,
    structured: dict | None = None,
) -> tuple[str, str, int | None]:
    """Ingest one worker task's report into the DB.

    ``structured`` is the worker's ``recipe__final_output`` dict — the normal
    path: persist its findings and render a markdown copy to ``report_path``
    for reviewers. When it is None (a salvage-write attempt wrote the report
    to disk instead), ingest the markdown report at ``report_path``.

    Returns the dispatcher's view of the task: ``(verdict, title, finding_id)``.
    """
    if structured is not None:
        return _ingest_structured_via_worker(
            conn, run_id, report_path, now, structured,
            task_id=task_id, anchor_file=anchor_file, run_dir=run_dir,
        )
    if not report_path.exists():
        raise ValueError(f"no ingestable report at {report_path}")
    return _ingest_one_file(
        conn, run_id, report_path, now, task_id=task_id, anchor_file=anchor_file,
    )


def _ingest_one_file(
    conn,
    run_id: str,
    report_path: Path,
    now: str,
    task_id: int | None = None,
    anchor_file: str | None = None,
) -> tuple[str, str, int | None]:
    """Insert a single ``worker_reports`` row plus (when verdict=finding) a
    ``findings`` row. Caller handles path resolution and dedup; this helper
    handles one report file."""
    metadata, body = load_report(report_path)

    # Prefer caller-supplied values; fall back to what the report itself provided.
    effective_task_id = task_id if task_id is not None else metadata.get("task_id")
    effective_anchor_file = anchor_file if anchor_file is not None else metadata.get("anchor_file", "")
    confidence = metadata.get("confidence", "high")

    # Reject the step-0 non-code early-exit stub when the anchor is actually
    # a code file. Observed pattern (2026-04-21, coriolix-3.0.1 run): the
    # worker runs a full real investigation on a `.py` / `.js` file,
    # correctly concludes no-finding, but then writes the step-0 stub to
    # disk because it's the only verbatim YAML+body template in the prompt.
    # The stub's Evidence section ("N/A — anchor type out of scope") masks
    # the real sink enumeration the worker produced, so downstream can't
    # tell a shallow miss from a rigorous no-finding. Raising here makes
    # the caller's exception handler requeue the task (up to max_attempts)
    # for a retry against the now-fixed prompt.
    title = str(metadata.get("title", "") or "")
    summary = (body or "").lstrip()
    if (
        title.startswith(NONCODE_STUB_TITLE)
        and effective_anchor_file
        and not is_noncode_anchor(effective_anchor_file)
    ):
        raise ValueError(
            f"worker report uses the non-code early-exit stub "
            f"(title={title!r}) but anchor "
            f"{effective_anchor_file!r} is a code file; the "
            f"investigation's real evidence was lost — requeue"
        )
    # Defense-in-depth: even if the title drifts, the stub's summary
    # body is diagnostic. Catch that shape too, for the same reason.
    if (
        summary.startswith(NONCODE_STUB_SUMMARY_PREFIX)
        and effective_anchor_file
        and not is_noncode_anchor(effective_anchor_file)
    ):
        raise ValueError(
            f"worker report body matches the non-code early-exit "
            f"stub summary but anchor {effective_anchor_file!r} is "
            f"a code file; requeue"
        )

    conn.execute(
        """
        INSERT INTO worker_reports
        (task_id, report_path, title, verdict, severity, confidence, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(effective_task_id) if effective_task_id is not None else None,
            str(report_path),
            str(metadata["title"]),
            str(metadata["verdict"]),
            str(metadata["severity"]),
            confidence,
            json.dumps(metadata, sort_keys=True),
            now,
        ),
    )
    report_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    if metadata["verdict"] == "finding":
        finding_status = "candidate"
    else:
        finding_status = None  # no-finding — don't insert into findings table

    if finding_status is not None:
        related_files = list(metadata.get("related_files", []))
        fingerprint = finding_fingerprint(
            str(metadata["title"]),
            str(metadata["finding_type"]),
            str(effective_anchor_file),
            related_files,
        )

        conn.execute(
            """
            INSERT INTO findings
            (task_id, report_id, title, finding_type, severity, confidence, status, anchor_file, related_files_json, fingerprint, summary, dynamic_evidence, cwe, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(effective_task_id) if effective_task_id is not None else None,
                report_id,
                str(metadata["title"]),
                str(metadata["finding_type"]),
                str(metadata["severity"]),
                confidence,
                finding_status,
                str(effective_anchor_file),
                json.dumps(related_files, sort_keys=True),
                fingerprint,
                extract_summary(body),
                0,
                metadata.get("cwe") or None,  # "" → NULL; match ingest_worker_report.py
                now,
            ),
        )
        finding_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    else:
        finding_id = None
    return str(metadata["verdict"]), str(metadata["title"]), finding_id


# ---------------------------------------------------------------------------
# Worker prompt rendering
# ---------------------------------------------------------------------------


def render_worker_prompt(
    payload: dict,
    framework_hints: tuple[str, ...] | list[str] = (),
    salvage_prior_analysis: str | None = None,
    cpg_enabled: bool | None = None,
) -> str:
    """Render the self-contained worker prompt.

    ``cpg_enabled`` controls whether CPG navigation guidance is appended.
    Pass ``True``/``False`` to force; pass ``None`` (default) to fall back
    to the legacy env probe (``RVH_CPG_ENDPOINT`` set ⇒ enabled). The
    autonomous runner passes the explicit per-anchor decision so shell
    anchors in a Python-only CPG don't get CPG mentions in their prompts.

    Recovery mode:

    - ``salvage_prior_analysis`` set: the prior attempt produced an
      analysis as inline text but skipped the `write` call. Renders
      ``salvage_write.md`` with that text baked in; the worker writes the
      parent report only, no re-investigation.
    """
    import os
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from prompts.loader import load_prompt

    if cpg_enabled is None:
        cpg_enabled = bool(os.environ.get("RVH_CPG_ENDPOINT"))

    # Workers see the repo at /workspace; give the model an absolute path.
    anchor_path = payload["anchor_file"]
    if not anchor_path.startswith("/"):
        anchor_path = f"/workspace/{anchor_path}"

    if salvage_prior_analysis is not None:
        return load_prompt(
            "salvage_write",
            cpg_enabled=False,  # salvage prompt is self-contained
            anchor_path=anchor_path,
            task_id=payload["task_id"],
            report_path=payload["report_path"],
            prior_analysis=salvage_prior_analysis,
        )

    build_environment_block = SANDBOX_ENVIRONMENT_BLOCK

    # Language-specific module — composed in based on the anchor file's
    # extension. Each module names bug classes characteristic of that
    # language AND defenses idiomatic to it (so the worker doesn't
    # over-report "missing ORM parameterization" on a codebase that uses
    # SQLAlchemy, etc.). Graceful: if no module file exists for the
    # detected language, the base prompt runs alone.
    language_module_block = _compose_language_module(
        payload["anchor_file"],
        cpg_enabled=cpg_enabled,
        framework_hints=framework_hints,
    )

    step0_block = _build_step0_block(payload["anchor_file"])
    region_block = _build_region_block(payload)

    # Wall-clock budget seconds for this priority, stated in the prompt so
    # the worker can pace itself. Absent / None / 0 → render `unknown`.
    _budget_s = payload.get("budget_seconds")
    if _budget_s is None or (isinstance(_budget_s, int) and _budget_s <= 0):
        budget_block = "- Budget: unknown"
    else:
        budget_block = f"- Budget: {int(_budget_s)}s"

    return load_prompt(
        "worker",
        cpg_enabled=cpg_enabled,
        anchor_path=anchor_path,
        task_id=payload["task_id"],
        report_path=payload["report_path"],
        priority=payload.get("priority", "unknown"),
        budget_block=budget_block,
        build_environment_block=build_environment_block,
        language_module_block=language_module_block,
        step0_block=step0_block,
        region_block=region_block,
    )


# Anchor file extension → language module name. Keep this small and
# obvious; add entries as new language modules land in prompts/.
_EXT_TO_LANG = {
    ".go": "go",
    ".py": "python", ".pyi": "python",
    ".c": "cpp", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp",
    ".cxx": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".hh": "cpp",
    ".rs": "rust",
    ".java": "java",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "ts", ".tsx": "ts",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "bash", ".bash": "bash",
    ".pl": "perl", ".pm": "perl",
    # Embedded source: components and views in their host language, and
    # zeek's binpac parsers and builtins, whose bodies are C++.
    ".vue": "js", ".svelte": "js",
    ".ctp": "php", ".phtml": "php", ".inc": "php",
    ".erb": "ruby",
    ".pac": "cpp", ".bif": "cpp",
}


def _compose_language_module(
    anchor_file: str,
    cpg_enabled: bool = False,
    framework_hints: tuple[str, ...] | list[str] = (),
) -> str:
    """Load prompts/worker_<lang>.md for the anchor file's language.

    Returns the module text wrapped with a leading+trailing blank line so it
    fits cleanly into the base prompt, or an empty string if no module
    exists for this language (graceful fallback — base prompt runs alone).

    When ``cpg_enabled=True``, a sibling ``worker_<lang>_cpg.md`` is appended
    after the base language module if it exists.

    When ``framework_hints`` names one or more detected web frameworks (e.g.
    ``("django",)``), per-framework snippets at ``worker_<lang>_<fw>.md`` are
    appended after the language module so the worker gets concrete
    gate-symbol guidance for the framework actually in use. Hints with no
    matching snippet file are silently skipped — the language module's
    abstract framework section still applies.
    """
    from pathlib import Path as _P
    ext = _P(anchor_file).suffix.lower()
    lang = _EXT_TO_LANG.get(ext)
    if not lang:
        return ""
    prompts_dir = _P(__file__).resolve().parent.parent / "prompts"
    module_path = prompts_dir / f"worker_{lang}.md"
    if not module_path.exists():
        return ""
    body = module_path.read_text(encoding="utf-8").rstrip()
    if cpg_enabled:
        cpg_module_path = prompts_dir / f"worker_{lang}_cpg.md"
        if cpg_module_path.exists():
            body = (
                body
                + "\n\n"
                + cpg_module_path.read_text(encoding="utf-8").rstrip()
            )
    for fw in framework_hints:
        if not fw or not isinstance(fw, str):
            continue
        fw_path = prompts_dir / f"worker_{lang}_{fw}.md"
        if fw_path.exists():
            body = body + "\n\n" + fw_path.read_text(encoding="utf-8").rstrip()
    return "\n" + body + "\n"


def safe_container_name(
    run_prefix: str, container_type: str, anchor_file: str, task_id: int
) -> str:
    """Build a docker container name like ``rvh-pelican-1a2b3c-w-broker-client.go-46``.

    Uses repo + type + first-path-segment + filename + task_id so `docker ps`
    output is scannable. Mid-path segments are dropped to bound the name length
    on deeply-nested anchors (e.g. ``web_ui/frontend/app/.../Token.tsx`` →
    ``web_ui-Token.tsx``).

    container_type is a short tag like ``w`` (worker).
    """
    parts = [p for p in anchor_file.replace(":", "/").split("/") if p]
    if len(parts) >= 2:
        anchor_part = f"{parts[0]}-{parts[-1]}"
    elif parts:
        anchor_part = parts[0]
    else:
        anchor_part = "unknown"
    name = f"{run_prefix}-{container_type}-{anchor_part}-{task_id}"
    # Docker name regex: [a-zA-Z0-9][a-zA-Z0-9_.-]*
    name = re.sub(r"[^a-zA-Z0-9_.-]", "-", name)
    name = re.sub(r"^[^a-zA-Z0-9]+", "", name)
    return name


def render_per_task_worker_recipe(
    rendered_prompt: str,
    base_recipe_path: Path,
) -> str:
    """Bake the rendered worker prompt into a goose recipe.

    Goose 1.32 cannot accept multi-line ``--params`` values: MiniJinja
    substitutes the value verbatim into the YAML literal-block at
    column 2 indent, but only the first line lands at the right indent
    — every subsequent line stays at column 0 and breaks the YAML
    re-parse. The workaround is to pre-render the recipe in Python:
    drop the ``rendered_prompt`` parameter declaration, set
    ``recipe['prompt']`` to the rendered string, and let PyYAML emit a
    properly-indented literal block.

    The launcher writes the resulting YAML to a per-task path (inside
    the ``/output`` bind-mount); ``goose run --recipe <path>`` then
    receives the prompt with no ``--params`` flag.
    """
    import yaml
    recipe = yaml.safe_load(base_recipe_path.read_text(encoding="utf-8"))
    recipe.pop("parameters", None)
    # Goose MiniJinja-parses the recipe prompt body. Worker prompts ship
    # LLM-facing SQLi examples like `f"SELECT {{user}}"` (worker_python.md);
    # without {% raw %} those `{{...}}` pairs trip
    # "Missing definitions for parameters in the recipe file: user, var"
    # and the recipe is rejected before any LLM call.
    if "{% endraw %}" in rendered_prompt:
        raise ValueError(
            "rendered prompt contains literal '{% endraw %}', which would "
            "terminate our raw-wrapper early; refusing to bake recipe"
        )
    # MiniJinja's `{% raw %}` block has a parser bug: it still tokenizes
    # `{{...}}` inside the block and rejects malformed expressions like
    # `{{$where}}`, `{{}}`, `{{absent, present}}`. Observed 2026-04-27
    # on JS chart files (E_*DataHandler.js) — every JS task failed in
    # ~0.6s with "Invalid recipe: syntax error: unknown statement endraw"
    # because worker_js.md has NoSQL examples like `{{$where}}` that
    # the raw scanner couldn't accept. Insert U+200B (zero-width space)
    # between EVERY adjacent brace pair so MiniJinja sees individual
    # `{` / `}` chars and never a `{{` / `}}` token. U+200B is invisible
    # to the LLM so prompt meaning is preserved.
    #
    # Regex with lookahead, not str.replace: worker_js.md line 37 has
    # `{{{{user}}}}` (quad-brace, React/Vue example) and a plain
    # `replace("{{", ...)` is non-overlapping so it leaves new `{{`
    # tokens behind on odd-length runs.
    safe_prompt = re.sub(r"(?<=\{)(?=\{)", "​", rendered_prompt)
    safe_prompt = re.sub(r"(?<=\})(?=\})", "​", safe_prompt)
    recipe["prompt"] = "{% raw %}\n" + safe_prompt + "\n{% endraw %}"
    return yaml.safe_dump(
        recipe,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10**9,  # disable PyYAML auto-wrapping; preserve our line breaks
    )


# ---------------------------------------------------------------------------
# Worker launch / collect
# ---------------------------------------------------------------------------

WORKER_MAX_TURNS = "2000"
WORKER_MAX_TOOL_REPETITIONS = "50"
GOOSE_CONTEXT_LIMIT = "256000"


def build_goose_inner_args(*, model: str, recipe_path: str, cpg_enabled: bool) -> list[str]:
    """goose's argv (no docker prefix) for one worker task.

    ``--no-profile`` skips goose's default extensions (25 tools, several of
    them distracting); ``developer`` (shell/write/edit) and ``todo`` are the
    builtins the worker needs — without ``todo`` the model's planning leaks
    into narration and a few percent of tasks loop. ``--max-turns`` is high
    because large anchors legitimately need hundreds of turns; runaway loops
    are bounded by ``--max-tool-repetitions`` instead. The CPG MCP extension
    is added only when the anchor's language is covered by the CPG.
    """
    cmd = [
        "goose", "run", "--provider", "openai", "--no-session", "--no-profile",
        "--output-format", "json",
        "--max-turns", WORKER_MAX_TURNS,
        "--max-tool-repetitions", WORKER_MAX_TOOL_REPETITIONS,
        "--recipe", recipe_path,
        "--model", model,
        "--with-builtin", "developer,todo",
    ]
    if cpg_enabled:
        cmd += ["--with-extension", "python3 /opt/rvh/cpg_mcp_server.py"]
    return cmd


WORKER_MEMORY = "8g"
WORKER_PIDS_LIMIT = "512"


def launch_worker_async(
    repo_root: Path,
    prompt: str,
    task_dir: Path,
    *,
    image: str,
    network: str,
    worker_endpoint: str,
    worker_api_key: str,
    worker_model: str,
    container_name: str | None = None,
    cpg_endpoint: str | None = None,
) -> dict:
    """Start one goose worker in a sandboxed container and return its handle.

    ``task_dir`` holds this task's prompt, goose recipe, system prompt and
    logs, and is the container's only writable mount (``/output``). The repo
    is mounted read-only at ``/workspace`` with ``.rvh-runs`` masked, so a
    worker can't read the run database or other tasks' reports. The container
    joins only the run's internal ``network`` (gateway + CPG), drops all
    capabilities, and runs as the invoking user so ``/output`` files are
    theirs.
    """
    ensure_dir(task_dir)
    (task_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    stdout_path = task_dir / "stdout.txt"
    stderr_path = task_dir / "stderr.txt"
    # goose uses this file verbatim instead of its generic system.md; without
    # it the worker gets a general-purpose persona and loses the methodology.
    (task_dir / GOOSE_SYSTEM_PROMPT_FILENAME).write_text(GOOSE_SYSTEM_PROMPT, encoding="utf-8")
    recipe_name = f"{task_dir.name}-recipe.yaml"
    base_recipe = Path(__file__).resolve().parent.parent / "prompts" / "recipes" / "worker.yaml"
    (task_dir / recipe_name).write_text(
        render_per_task_worker_recipe(rendered_prompt=prompt, base_recipe_path=base_recipe),
        encoding="utf-8",
    )
    goose_inner = build_goose_inner_args(
        model=worker_model, recipe_path=f"/output/{recipe_name}", cpg_enabled=bool(cpg_endpoint),
    )

    cmd = [
        "docker", "run", "--rm", "-i",
        "--network", network,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", WORKER_PIDS_LIMIT, "--memory", WORKER_MEMORY,
        "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
        "--mount", f"type=bind,src={repo_root},dst=/workspace,readonly",
        "--mount", f"type=tmpfs,dst=/workspace/{DEFAULT_RUNS_DIR}",
        "--mount", f"type=bind,src={task_dir},dst=/output",
        "-e", f"GOOSE_SYSTEM_PROMPT_FILE_PATH=/output/{GOOSE_SYSTEM_PROMPT_FILENAME}",
    ]
    if cpg_endpoint:
        cmd += ["-e", f"RVH_CPG_ENDPOINT={cpg_endpoint}"]
    if container_name:
        cmd += ["--name", container_name]
    cmd += [
        "-e", f"OPENAI_HOST={worker_endpoint}",
        "-e", f"OPENAI_API_KEY={worker_api_key}",
        # Switch off goose's implicit extra model calls: auto-compaction,
        # tool-pair summarization, and session naming.
        "-e", "GOOSE_AUTO_COMPACT_THRESHOLD=0.0",
        "-e", "GOOSE_TOOL_PAIR_SUMMARIZATION=false",
        "-e", "GOOSE_DISABLE_SESSION_NAMING=true",
        "-e", f"GOOSE_CONTEXT_LIMIT={GOOSE_CONTEXT_LIMIT}",
        "-e", "OPENAI_TIMEOUT=1800",
        image,
        # goose has no --cd flag; shlex.join quotes the --with-extension value.
        "bash", "-c", "cd /workspace && " + shlex.join(goose_inner),
    ]

    if container_name:
        # A killed earlier attempt can leave a same-named container behind,
        # which makes `docker run --name` fail with a name conflict.
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=stdout_file, stderr=stderr_file, text=True,
        )
    except BaseException:
        stdout_file.close()
        stderr_file.close()
        raise
    return {
        "process": proc,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_file": stdout_file,
        "stderr_file": stderr_file,
        "launched_at": time.time(),
        "container_name": container_name,
    }


def collect_worker_async(handle: dict) -> tuple[int, Path, Path, float]:
    """Wait for the worker to exit; return (returncode, stdout, stderr, elapsed)."""
    proc = handle["process"]
    proc.wait()
    # Close our ends so buffered output is on disk before callers read it.
    for key in ("stdout_file", "stderr_file"):
        f = handle.get(key)
        if f is not None and not f.closed:
            f.close()
    elapsed = round(time.time() - handle["launched_at"], 3)
    return proc.returncode, handle["stdout_path"], handle["stderr_path"], elapsed


def poll_worker_async(handle: dict) -> bool:
    """True iff the worker process has finished. Non-blocking."""
    return handle["process"].poll() is not None


def terminate_worker(handle: dict, *, grace_seconds: int = 10) -> None:
    """Stop a running worker and remove its container.

    SIGTERM the host-side `docker run`, wait `grace_seconds`, SIGKILL on
    timeout, then `docker rm -f` the container by name: killing the
    `docker run` CLI does not stop the container, and a surviving goose keeps
    holding model slots.
    """
    proc = handle.get("process")
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
    container_name = handle.get("container_name")
    if container_name:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
