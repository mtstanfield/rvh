#!/usr/bin/env python3
"""rvh engine — rolling priority-queue dispatch of containerized goose workers."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from common import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RUNS_DIR,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_PENDING,
    TASK_REQUEUED,
    TASK_RESERVED,
    add_seconds,
    append_event,
    connect_db,
    debug_log,
    extract_last_assistant_text,
    load_repo_profile,
    read_config,
    repo_path,
    require_run,
    start_caffeinate,
    task_with_file,
    update_run_timestamp,
    utc_now,
)
from cpg_build import anchor_supported_by_cpg
from sandbox import WORKER_API_KEY, WORKER_ENDPOINT, run_prefix
from worker import (
    DEFAULT_OWNER,
    DEFAULT_STOP_FILE,
    append_batch_journal,
    collect_worker_async,
    extract_final_output,
    finalize_task,
    ingest_report,
    launch_worker_async,
    log_progress,
    poll_worker_async,
    render_worker_prompt,
    requeue_task_without_penalty,
    reserve_exact_task,
    safe_container_name,
    start_task,
    terminate_worker,
)


# ---------------------------------------------------------------------------
# Task picking — priority DESC, one at a time under rolling dispatch
# (kept as pick_next_batch for schema/caller stability; always called with
# limit=1 from the rolling dispatcher)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Per-task wall-clock budget — score-tier cap on worker runtime
# ---------------------------------------------------------------------------
#
# The worker has no built-in wall-clock cap (OPENAI_TIMEOUT is per call), so
# the dispatcher kills a worker whose task outlives its priority's budget.
# Priority-5 anchors (large handler files with a broad sink surface) get an
# hour; everything else gets 30 minutes, which covers the typical 200-700 s
# investigation with room for the full enumeration battery.


def worker_wall_budget_seconds(priority: int) -> int:
    """Wall-clock seconds a worker may run on a task of this priority."""
    return 3600 if priority >= 5 else 1800


def pick_next_batch(conn, min_priority: int, limit: int) -> list[dict]:
    """Select the next N pending/requeued tasks ordered by priority DESC, id ASC."""
    rows = conn.execute(
        """
        SELECT tasks.id AS task_id, tasks.priority, tasks.attempt_count, files.path
        FROM tasks
        JOIN files ON files.id = tasks.file_id
        WHERE tasks.status IN (?, ?)
          AND tasks.priority >= ?
        ORDER BY tasks.priority DESC,
                 COALESCE(tasks.parent_task_id, tasks.id) ASC,
                 tasks.id ASC
        LIMIT ?
        """,
        (TASK_PENDING, TASK_REQUEUED, min_priority, max(1, limit)),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Salvage sidecar plumbing
# ---------------------------------------------------------------------------

def _salvage_sidecar_path(run_dir: Path, task_id: int) -> Path:
    """Resolve the salvage sidecar path for ``task_id`` under ``run_dir``."""
    return run_dir / "reports" / ".salvage" / f"task-{task_id}.txt"


def stash_salvage_sidecar(run_dir: Path, task_id: int, stdout_path: Path) -> int:
    """Pull the last assistant turn from the worker's JSON stdout and stash
    it as a salvage sidecar. Returns the number of characters stashed, or 0
    if extraction failed (no JSON, no assistant text, OS error). Idempotent
    — overwrites any prior sidecar for the same task.

    Used by the autonomous runner in two places: when a worker exits 0 but
    never reported, and when a worker is SIGKILLed for budget exhaustion. In
    both cases the next attempt runs the salvage_write prompt, which tends to
    finish in 40-50s.
    """
    prior = extract_last_assistant_text(stdout_path)
    if not prior:
        return 0
    sidecar = _salvage_sidecar_path(run_dir, task_id)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(prior, encoding="utf-8")
    return len(prior)


def clear_salvage_sidecar(run_dir: Path, task_id: int) -> None:
    """Remove the salvage sidecar for ``task_id`` if present. Best-effort —
    ignores OS errors. Called when a task reaches a terminal state so a
    stale sidecar doesn't leak into a future re-priority run."""
    sidecar = _salvage_sidecar_path(run_dir, task_id)
    if sidecar.exists():
        try:
            sidecar.unlink()
        except OSError:
            pass


def requeue_interrupted_task(args, run_dir: Path, state: dict, reason: str) -> dict:
    """Manual-stop drain (Ctrl+C / stop-file): terminate the in-flight worker and
    requeue its task for a CLEAN re-run on resume — never salvage.

    The normal drain path (``collect_task``) blocks on ``proc.wait()`` and runs
    the completion logic, which stashes a salvage sidecar when an interrupted
    worker has no report yet — so resume re-enters salvage-write mode on partial
    output instead of re-investigating. This path instead stops the worker now,
    clears any salvage sidecar, and requeues *without penalty* (reversing
    the attempt increment), so the next dispatch is a full fresh investigation.
    Used only for operator-initiated stops; planned boundaries (max-tasks /
    max-runtime) still drain to natural completion where salvage is appropriate."""
    reservation = state["reservation"]
    task_id = reservation["task_id"]
    try:
        terminate_worker(state["handle"], grace_seconds=10)
    except Exception as exc:  # one worker's teardown must not block the rest
        log_progress(f"[rvh] interrupt: terminate_worker task={task_id}: {exc}")
    clear_salvage_sidecar(run_dir, task_id)
    final_status = TASK_REQUEUED
    with connect_db(run_dir) as conn:
        try:
            requeue_task_without_penalty(
                conn, task_id, args.owner, state["dispatch_id"], reason, utc_now()
            )
        except Exception as exc:
            log_progress(f"[rvh] interrupt: requeue task={task_id}: {exc}")
            final_status = "interrupt-requeue-failed"
    log_progress(
        f"[rvh] interrupted task={task_id} requeued for a clean re-run on resume"
    )
    return {
        "batch_id": state["dispatch_id"],
        "task_id": task_id,
        "anchor_file": reservation["path"],
        "returncode": None,
        "elapsed_seconds": None,
        "final_status": final_status,
        "verdict": "interrupted",
    }


def reclaim_orphaned_tasks(run_dir: Path) -> int:
    """Resume recovery: re-pend tasks left ``reserved``/``in_progress`` by a prior
    engine that exited uncleanly (double Ctrl+C, SIGKILL, crash) — the graceful
    path requeues its own in-flight task, but a hard exit can't.

    Runs at engine startup BEFORE any worker is launched, so under the
    single-run-per-repo model every such row is necessarily an orphan of a dead
    run — safe to reclaim without waiting for lease expiry (which would leave a
    freshly-killed task stuck until its lease aged out). Requeues clean (reversing
    the dispatch's attempt increment, no penalty) and clears any salvage
    sidecars so the re-run is a full fresh investigation, not salvage-write.
    Returns the number reclaimed."""
    now = utc_now()
    with connect_db(run_dir) as conn:
        task_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM tasks WHERE status IN (?, ?)",
                (TASK_RESERVED, TASK_IN_PROGRESS),
            )
        ]
        if task_ids:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, owner = NULL, owner_session = NULL,
                    reserved_at = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    completed_at = NULL, attempt_count = MAX(0, attempt_count - 1),
                    notes = ?, updated_at = ?
                WHERE status IN (?, ?)
                """,
                (TASK_REQUEUED, "reclaimed orphaned in-flight task on resume",
                 now, TASK_RESERVED, TASK_IN_PROGRESS),
            )
    for tid in task_ids:
        clear_salvage_sidecar(run_dir, tid)
    if task_ids:
        log_progress(
            f"[rvh] resume: reclaimed {len(task_ids)} orphaned in-flight "
            f"task(s) from an unclean prior exit — requeued for clean re-run"
        )
    return len(task_ids)


# ---------------------------------------------------------------------------
# Stop condition check
# ---------------------------------------------------------------------------

def check_stop(stop_file: Path, tasks_done: int, max_tasks: int | None) -> str | None:
    if stop_file.exists():
        return f"stop file present: {stop_file}"
    if max_tasks is not None and tasks_done >= max_tasks:
        return f"max tasks reached: {max_tasks}"
    return None

# ---------------------------------------------------------------------------
# Task execution — launch + collect (called from the rolling dispatcher)
# ---------------------------------------------------------------------------

def launch_task(
    args,
    run_dir: Path,
    repo_root: Path,
    task: dict,
    dispatch_id: str,
) -> dict | None:
    """Reserve, start, build prompt, and launch one worker. Returns a
    WorkerState dict with keys {task_id, reservation, dispatch_payload,
    handle, dispatch_id}, or None if the DB reservation was lost to a
    concurrent claimer."""

    config = read_config(run_dir)
    lease_seconds = int(config.get("lease_seconds", DEFAULT_LEASE_SECONDS))
    max_attempts = int(config.get("max_attempts", DEFAULT_MAX_ATTEMPTS))

    now = utc_now()
    lease_expires_at = add_seconds(now, max(60, lease_seconds))

    with connect_db(run_dir) as conn:
        run = require_run(conn)
        run_id = run["id"]

        reservation = reserve_exact_task(
            conn,
            int(task["task_id"]),
            args.owner,
            dispatch_id,
            lease_expires_at,
            max_attempts,
            now,
        )
        if reservation is None:
            return None
        start_task(conn, reservation["task_id"], args.owner, dispatch_id, lease_expires_at, now)
        update_run_timestamp(conn, run_id)

    # Each task gets its own directory, mounted as the worker's /output.
    task_dir = run_dir / "tasks" / f"{dispatch_id}-task-{reservation['task_id']}"
    report_filename = f"task-{reservation['task_id']}.md"
    host_report_path = str(task_dir / report_filename)
    prompt_report_path = f"/output/{report_filename}"

    # Wall-clock budget for this priority — the dispatcher kills the
    # container when it elapses; the prompt states it so the worker can pace.
    _budget_seconds = worker_wall_budget_seconds(int(reservation["priority"]))
    dispatch_payload = {
        "task_id": reservation["task_id"],
        "owner": args.owner,
        "owner_session": dispatch_id,
        "anchor_file": reservation["path"],
        "anchor_start_line": reservation["anchor_start_line"],
        "anchor_end_line": reservation["anchor_end_line"],
        "priority": reservation["priority"],
        "budget_seconds": _budget_seconds,
        "repo_root": str(repo_root),
        "run_dir": str(run_dir),
        "report_path": prompt_report_path,
        "host_report_path": host_report_path,
        "worker_instructions": [
            "Review the anchor file first, then inspect neighboring context as needed.",
            "Produce a defensive maintainer-facing report only.",
            "Do not mark the finding as validated; parent triage decides that.",
            "Do not write exploit code.",
        ],
    }

    # Detected web frameworks land in repo_profile.json (derive_repo_profile
    # populates framework_hints). Forward them so the worker prompt's per-
    # language module can include framework-specific gate-symbol guidance.
    repo_profile = load_repo_profile(run_dir)
    framework_hints = tuple(repo_profile.get("framework_hints") or ())

    # Salvage mode: a prior attempt produced an analysis as inline assistant
    # text but never called `write` to materialize the report. The previous
    # collect_task wrote the extracted text to this sidecar; if it exists,
    # render the salvage prompt (format-and-write only — no investigation,
    # no subagent delegation) instead of the full worker prompt.
    salvage_path = run_dir / "reports" / ".salvage" / f"task-{reservation['task_id']}.txt"
    salvage_prior_analysis = None
    if salvage_path.exists():
        try:
            salvage_prior_analysis = salvage_path.read_text(encoding="utf-8")
            log_progress(
                f"[rvh] salvage-write mode for task={reservation['task_id']} "
                f"anchor={reservation['path']} "
                f"({len(salvage_prior_analysis)} chars of prior analysis)"
            )
            # One-shot: delete the sidecar now so a salvage failure either
            # stashes a fresh extract from the salvage attempt's stdout, or
            # falls back to a full re-run on the next attempt.
            try:
                salvage_path.unlink()
            except OSError:
                pass
        except OSError as exc:
            log_progress(
                f"[rvh] salvage sidecar unreadable for task={reservation['task_id']}: "
                f"{exc} — falling back to full re-run"
            )

    # Per-anchor CPG gating: even when the sidecar is up, only expose CPG
    # tools to workers whose anchor language is covered by the built CPG.
    # Shell anchors in a Python-only CPG (and similar mismatches) drove the
    # 2026-05-06 coriolix budget kill on killServices.sh — the worker burned
    # the budget retrying inspect() calls that returned "no node at that
    # location". One decision propagates to the prompt rendering and the
    # container's MCP wiring so prompt mentions match available tools.
    sidecar_endpoint = os.environ.get("RVH_CPG_ENDPOINT") or None
    cpg_supported_for_anchor = bool(sidecar_endpoint) and anchor_supported_by_cpg(
        reservation["path"], run_dir / "cpg"
    )
    effective_cpg_endpoint = sidecar_endpoint if cpg_supported_for_anchor else None

    prompt = render_worker_prompt(
        dispatch_payload,
        framework_hints=framework_hints,
        salvage_prior_analysis=salvage_prior_analysis,
        cpg_enabled=cpg_supported_for_anchor,
    )

    if args.debug:
        task_id_int = reservation["task_id"]
        debug_label = f"task{task_id_int:04d}-input"
        header = (
            f"# Debug: worker task {task_id_int}\n"
            f"# Time: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
            f"# Anchor: {reservation['path']}\n"
            f"# Priority: {reservation['priority']}\n"
            f"# Session: {dispatch_id}\n\n"
        )
        debug_log(run_dir, "worker", debug_label, header + prompt)

    handle = launch_worker_async(
        repo_root,
        prompt,
        task_dir,
        image=args.image,
        network=args.network,
        worker_endpoint=WORKER_ENDPOINT,
        worker_api_key=WORKER_API_KEY,
        worker_model=args.model,
        container_name=safe_container_name(
            run_prefix(repo_root), "w", reservation["path"], reservation["task_id"]
        ),
        cpg_endpoint=effective_cpg_endpoint,
    )

    # Recovery flag drives the budget-kill / expected-report-missing
    # salvage stash logic in collect_task: a recovery attempt's stdout is
    # partial recovery output (e.g. mid-write of a finding markdown), not a
    # standalone analysis. Re-stashing it as a salvage sidecar would feed
    # the next attempt to salvage_write.md, which writes the *parent* —
    # the result is the parent report getting overwritten with garbage.
    # When this flag is set, the budget-kill / missing-report branches
    # skip the stash; the next attempt falls back to a full re-run
    # (still bounded by max_attempts).
    recovery_mode = "salvage" if salvage_prior_analysis is not None else None

    return {
        "task_id": reservation["task_id"],
        "reservation": reservation,
        "dispatch_payload": dispatch_payload,
        "handle": handle,
        "dispatch_id": dispatch_id,
        "recovery_mode": recovery_mode,
    }


def collect_task(
    args,
    run_dir: Path,
    state: dict,
    max_attempts: int,
) -> dict:
    """Collect a finished worker, finalize its DB row, and return the
    journal record. Caller must have confirmed poll_worker_async(state['handle'])
    returned True (otherwise this will block in collect_worker_async).

    `max_attempts` is read in the caller so the rolling dispatcher doesn't
    reopen the config JSON once per task."""

    reservation = state["reservation"]
    dispatch_payload = state["dispatch_payload"]
    dispatch_id = state["dispatch_id"]
    handle = state["handle"]

    returncode, stdout_path, stderr_path, elapsed = collect_worker_async(handle)

    log_progress(
        f"[rvh] worker finished "
        f"task={reservation['task_id']} anchor={reservation['path']} "
        f"returncode={returncode} elapsed={round(elapsed, 1)}s"
    )

    # A salvage-write worker writes its markdown into its own task dir; the
    # canonical copy (and structured ingest's rendering) lives in reports/.
    worker_report = Path(dispatch_payload["host_report_path"])
    report_path = run_dir / "reports" / worker_report.name
    final_status = TASK_FAILED
    final_notes = ""
    verdict = "no-report"
    title = ""
    finding_id = None
    error_text = None

    # Phase 4: open DB per-task for finalization
    with connect_db(run_dir) as conn:
        run = require_run(conn)
        finish_now = utc_now()
        try:
            if returncode != 0:
                raise RuntimeError(f"worker exited with {returncode}")

            # Two paths to a valid deliverable:
            #   1. Flat worker emitted recipe__final_output → structured dict.
            #      ingest_report(structured=...) persists it and renders the
            #      markdown file itself.
            #   2. The worker wrote the markdown to disk instead (the prompts'
            #      fallback when recipe__final_output isn't registered) →
            #      ingest_report(structured=None) reads the frontmatter.
            # Only raise "report missing" when BOTH are absent.
            structured = extract_final_output(stdout_path)
            if structured is None and not worker_report.exists():
                # Worker exited cleanly but produced neither structured
                # output nor a markdown file. If the final assistant turn
                # contained an analysis, stash it as a salvage sidecar so
                # the next attempt renders salvage_write.md (format-and-
                # write only) instead of redoing the whole investigation.
                # Sidecar absence ⇒ next attempt falls back to a full
                # re-run.
                #
                # Skip the stash when this attempt was itself a salvage —
                # the broken salvage attempt's stdout is partial-write
                # output, not a fresh analysis, and overwriting the
                # original sidecar with it would feed the next attempt
                # garbage.
                if state.get("recovery_mode") == "salvage":
                    log_progress(
                        f"[rvh] salvage attempt produced no parent "
                        f"for task={reservation['task_id']}; not re-stashing "
                        f"— next attempt is a full re-run"
                    )
                else:
                    stashed = stash_salvage_sidecar(
                        run_dir, reservation["task_id"], stdout_path
                    )
                    if stashed:
                        log_progress(
                            f"[rvh] stashed salvage sidecar "
                            f"task={reservation['task_id']} ({stashed} chars) "
                            f"— next attempt is salvage-write"
                        )
                raise RuntimeError(f"expected worker report missing: {worker_report}")

            if structured is None:
                log_progress(
                    f"[ingest] no recipe__final_output captured for task "
                    f"{reservation['task_id']} — falling back to on-disk markdown"
                )
                if worker_report.resolve() != report_path.resolve():
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(worker_report, report_path)

            verdict, title, finding_id = ingest_report(
                conn, run["id"], report_path, finish_now,
                task_id=reservation["task_id"],
                anchor_file=reservation["path"],
                run_dir=run_dir,
                structured=structured,
            )

            # Successful ingest — clear any lingering salvage
            # sidecar (defensive; launch_task already deletes on use).
            clear_salvage_sidecar(run_dir, reservation["task_id"])

            final_status = TASK_COMPLETED
            if verdict == "finding":
                final_notes = f"finding: {title}"
                log_progress(
                    f"[rvh] FINDING #{finding_id}: {title} "
                    f"(anchor={reservation['path']})"
                )
            elif verdict == "no-finding":
                final_notes = "no-finding"
                log_progress(
                    f"[rvh] no-finding for {reservation['path']}"
                )
            else:
                final_notes = f"verdict: {verdict}"

            finalize_task(
                conn, reservation["task_id"], args.owner, dispatch_id,
                final_status, final_notes, None, finish_now,
            )
            update_run_timestamp(conn, run["id"])

        except Exception as exc:
            error_text = str(exc)
            task_row = task_with_file(conn, reservation["task_id"])
            at_limit = task_row and int(task_row["attempt_count"] or 0) >= max_attempts
            # Budget-killed tasks: stash whatever the worker had managed
            # to think before SIGKILL and requeue as a salvage attempt.
            # The salvage prompt is a tight format-and-write that fits
            # comfortably in any priority's budget (~40-50s observed).
            # When we're already at the attempt cap, fall through to
            # TASK_FAILED instead of looping.
            if state.get("budget_killed"):
                error_text = (
                    f"budget exhausted: priority={reservation['priority']} "
                    f"budget={state.get('budget_seconds')}s — terminated"
                )
                if not at_limit:
                    # Skip the stash when the budget-killed attempt was
                    # itself a salvage attempt. The
                    # extracted "last assistant turn" is mid-recovery
                    # output, not a standalone analysis — stashing it
                    # as a salvage sidecar would drive the next attempt
                    # into salvage_write against garbage and could
                    # Falling back to a full re-run is safer;
                    # max_attempts bounds the loop.
                    if state.get("recovery_mode"):
                        log_progress(
                            f"[rvh] budget kill of "
                            f"{state['recovery_mode']} attempt for "
                            f"task={reservation['task_id']}; not stashing "
                            f"a fresh salvage sidecar — next attempt is "
                            f"a full re-run"
                        )
                        stashed = 0
                    else:
                        stashed = stash_salvage_sidecar(
                            run_dir, reservation["task_id"], stdout_path
                        )
                        if stashed:
                            log_progress(
                                f"[rvh] budget kill — stashed salvage "
                                f"sidecar task={reservation['task_id']} "
                                f"({stashed} chars) — next attempt is salvage-write"
                            )
                        else:
                            log_progress(
                                f"[rvh] budget kill — no recoverable "
                                f"prior turn for task={reservation['task_id']}; "
                                f"next attempt is a full re-run"
                            )
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = ?, owner = NULL, owner_session = NULL,
                            reserved_at = NULL, lease_expires_at = NULL,
                            heartbeat_at = NULL, completed_at = NULL,
                            last_error = ?, notes = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (TASK_REQUEUED, error_text,
                         "requeued after budget exhaustion",
                         finish_now, reservation["task_id"]),
                    )
                    final_status = TASK_REQUEUED
                    final_notes = "requeued after budget exhaustion"
                else:
                    clear_salvage_sidecar(run_dir, reservation["task_id"])
                    finalize_task(
                        conn, reservation["task_id"], args.owner, dispatch_id,
                        TASK_FAILED,
                        "budget exhausted (max attempts reached)",
                        error_text, finish_now,
                    )
                    final_status = TASK_FAILED
                    final_notes = "budget exhausted (max attempts reached)"
            elif at_limit:
                # Permanent failure — clean up any salvage sidecar so they don't bleed into a future re-priority.
                clear_salvage_sidecar(run_dir, reservation["task_id"])
                finalize_task(
                    conn, reservation["task_id"], args.owner, dispatch_id,
                    TASK_FAILED, "autonomous worker failed (max attempts reached)",
                    error_text, finish_now,
                )
            else:
                log_progress(
                    f"[rvh] worker error for task={reservation['task_id']} "
                    f"anchor={reservation['path']} — requeuing: {error_text[:200]}"
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, owner = NULL, owner_session = NULL,
                        reserved_at = NULL, lease_expires_at = NULL,
                        heartbeat_at = NULL, completed_at = NULL,
                        last_error = ?, notes = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (TASK_REQUEUED, error_text, "requeued after worker error",
                     finish_now, reservation["task_id"]),
                )
                final_status = TASK_REQUEUED
                final_notes = "requeued after worker error"
            update_run_timestamp(conn, run["id"])

    return {
        "batch_id": dispatch_id,
        "task_id": reservation["task_id"],
        "anchor_file": reservation["path"],
        "returncode": returncode,
        "elapsed_seconds": elapsed,
        "final_status": final_status,
        "verdict": verdict,
        "title": title,
        "finding_id": finding_id,
        "report_path": str(report_path),
        "error": error_text,
    }


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="rvh engine — rolling priority-queue dispatch of goose workers",
    )
    parser.add_argument("--repo", required=True, help="Target repo path")
    parser.add_argument("--run-dir", default=None, help="Run directory (default: <repo>/.rvh-runs)")
    parser.add_argument("--parallelism", type=int, default=1, help="Max in-flight workers")
    parser.add_argument("--min-priority", type=int, default=3, help="Only dispatch tasks at or above this score")
    parser.add_argument("--max-tasks", type=int, default=None, help="Stop after this many tasks")
    parser.add_argument("--image", required=True, help="Worker image tag")
    parser.add_argument("--network", required=True, help="The run's internal Docker network")
    parser.add_argument("--model", required=True, help="Model the worker runs")
    parser.add_argument("--debug", action="store_true", help="Save worker prompts under logs/debug/")
    # Fixed identifiers, not user settings: kept on args because the task
    # lifecycle helpers take them from there.
    parser.set_defaults(owner=DEFAULT_OWNER, session_prefix="auto", stop_file=DEFAULT_STOP_FILE)
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    start_caffeinate()
    parser = build_parser()
    args = parser.parse_args()

    repo_root = repo_path(args.repo)
    run_dir = Path(args.run_dir) if args.run_dir else repo_root / DEFAULT_RUNS_DIR
    run_dir = run_dir.resolve()

    if not (run_dir / "state.db").exists():
        raise SystemExit(
            f"No state.db found in {run_dir}. "
            "Run ./rvh to bootstrap it first."
        )

    stop_file = run_dir / args.stop_file

    # Verify run exists and set to running
    with connect_db(run_dir) as conn:
        run = require_run(conn)
        run_id = run["id"]
        if run["status"] not in ("running", "scored"):
            log_progress(
                f"[rvh] run status is {run['status']!r} — "
                "expected running/scored"
            )
        conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
            ("running", utc_now(), run_id),
        )

    # Signal handling
    _shutdown = False

    def _handle_sigint(signum, frame):
        nonlocal _shutdown
        if _shutdown:
            raise SystemExit(1)
        _shutdown = True
        log_progress("\n[rvh] interrupt received — stopping after in-flight drain")

    signal.signal(signal.SIGINT, _handle_sigint)

    started_at = time.time()
    tasks_done = 0
    batch_number = 0
    stop_reason = None

    # Hoist max_attempts so collect_task doesn't re-read the config JSON
    # once per completion. (launch_task still reads config per spawn for
    # lease_seconds; not worth a bigger refactor right now.)
    max_attempts = int(read_config(run_dir).get("max_attempts", DEFAULT_MAX_ATTEMPTS))

    # Resume recovery: re-pend any task an unclean prior exit left reserved/
    # in_progress, BEFORE we launch a worker (so every such row is an orphan).
    reclaim_orphaned_tasks(run_dir)

    in_flight: dict[int, dict] = {}
    try:
        while True:
            if _shutdown:
                stop_reason = "interrupted by user (Ctrl+C)"
                break

            stop_reason = check_stop(stop_file, tasks_done, args.max_tasks)
            if stop_reason:
                break

            # Fill empty slots up to --parallelism
            while len(in_flight) < args.parallelism:
                with connect_db(run_dir) as conn:
                    next_batch = pick_next_batch(conn, args.min_priority, 1)
                if not next_batch:
                    break
                task = next_batch[0]
                batch_number += 1
                dispatch_id = f"{args.session_prefix}-{batch_number:04d}-{os.urandom(4).hex()}"
                state = launch_task(args, run_dir, repo_root, task, dispatch_id)
                if state is None:
                    continue  # reservation lost to another claimer; try next task
                in_flight[state["task_id"]] = state
                log_progress(
                    f"[rvh] dispatch spawned task={state['task_id']} "
                    f"anchor={state['reservation']['path']} "
                    f"in_flight={len(in_flight)}/{args.parallelism}"
                )

            # Nothing running and nothing to spawn → done
            if not in_flight:
                stop_reason = f"no pending tasks at priority >= {args.min_priority}"
                break

            # Enforce per-task wall budget (score-tier cap). Tasks that have
            # exceeded their priority's budget get SIGTERM'd; they fall through
            # to the drain below as completed processes and collect_task
            # records them as budget-exhausted. Done before poll_worker_async
            # so a budget-killed task gets reaped on this same iteration.
            now = time.time()
            for st in in_flight.values():
                if st.get("budget_killed"):
                    continue
                handle = st["handle"]
                priority = int(st["reservation"]["priority"])
                budget = worker_wall_budget_seconds(priority)
                elapsed = now - handle["launched_at"]
                if elapsed < budget:
                    continue
                proc = handle.get("process")
                if proc is None or proc.poll() is not None:
                    continue
                log_progress(
                    f"[rvh] worker budget exhausted "
                    f"task={st['task_id']} priority={priority} "
                    f"elapsed={int(elapsed)}s budget={budget}s — terminating"
                )
                terminate_worker(handle, grace_seconds=10)
                st["budget_killed"] = True
                st["budget_seconds"] = budget

            # Drain completions (non-blocking)
            done_ids = [tid for tid, st in in_flight.items() if poll_worker_async(st["handle"])]
            if not done_ids:
                time.sleep(0.2)
                continue

            for tid in done_ids:
                state = in_flight.pop(tid)
                record = collect_task(args, run_dir, state, max_attempts)
                append_batch_journal(run_dir, record)
                append_event(run_dir, {"event": "autonomous_batch_finished", **record})
                tasks_done += 1
                log_progress(
                    f"[rvh] dispatch drained task={tid} "
                    f"in_flight={len(in_flight)}/{args.parallelism} "
                    f"tasks_done={tasks_done}"
                )

            with connect_db(run_dir) as conn:
                conn.execute(
                    "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                    ("running", utc_now(), run_id),
                )

    finally:
        # Drain in-flight workers before shutting down the worker threads.
        # Under rolling dispatch the main loop can break with live subprocesses
        # (max_tasks / stop_file / Ctrl+C), unlike the old batch
        # model where run_batch blocked on all workers before returning. Not
        # draining here would orphan docker containers and leave their DB
        # rows in 'in_progress' until lease expiry, with no journal record.
        # For Ctrl+C: user can still hit Ctrl+C again to force-exit — the
        # signal handler upstream re-raises on re-entry.
        if in_flight:
            # Operator-initiated stop (Ctrl+C / stop-file): terminate the
            # in-flight worker(s) and requeue them for a clean re-run on resume
            # — never salvage. Planned boundaries (max-tasks) fall
            # through to the drain below, which finishes a near-done worker and
            # lets the legitimate salvage path run if it produced no report.
            manual_stop = _shutdown or (stop_reason or "").startswith("stop file")
            if manual_stop:
                log_progress(
                    f"[rvh] manual stop — requeuing {len(in_flight)} in-flight "
                    f"task(s) for a clean re-run on resume"
                )
                for tid in list(in_flight.keys()):
                    state = in_flight.pop(tid)
                    record = requeue_interrupted_task(
                        args, run_dir, state,
                        "interrupted by manual stop — requeued for clean re-run",
                    )
                    append_event(run_dir, {"event": "autonomous_task_interrupted", **record})
            else:
                log_progress(
                    f"[rvh] draining {len(in_flight)} in-flight worker(s) before exit"
                )
                for tid in list(in_flight.keys()):
                    state = in_flight.pop(tid)
                    try:
                        record = collect_task(args, run_dir, state, max_attempts)
                    except Exception as exc:
                        log_progress(
                            f"[rvh] drain: collect_task for task={tid} failed: {exc}"
                        )
                        continue
                    append_batch_journal(run_dir, record)
                    append_event(run_dir, {"event": "autonomous_batch_finished", **record})
                    tasks_done += 1

        # Set final run status
        if stop_reason:
            # Map stop_reason → (final_status, category) for runs.status and
            # the event log.
            if stop_reason.startswith("no pending tasks"):
                final_status = "completed"
                category = "repo_complete"
            elif stop_reason.startswith("max tasks reached"):
                final_status = "stopped"
                category = "task_budget_reached"
            elif stop_reason.startswith("stop file"):
                final_status = "stopped"
                category = "manual_stop"
            else:
                final_status = "stopped"
                category = "stopped"

            event_name = (
                "autonomous_runner_complete" if category == "repo_complete"
                else "autonomous_runner_stopped"
            )
            append_event(run_dir, {
                "event": event_name,
                "category": category,
                "reason": stop_reason,
                "tasks_done": tasks_done,
                "batches": batch_number,
                "elapsed_seconds": round(time.time() - started_at, 1),
            })

            with connect_db(run_dir) as conn:
                conn.execute(
                    "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                    (final_status, utc_now(), run_id),
                )

            log_progress(
                f"[rvh] v2 {final_status}: {stop_reason} "
                f"(tasks={tasks_done}, dispatches={batch_number}, "
                f"elapsed={round(time.time() - started_at, 1)}s)"
            )


if __name__ == "__main__":
    main()
