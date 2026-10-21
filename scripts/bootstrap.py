#!/usr/bin/env python3
"""Bootstrap an rvh run: init -> inventory -> analyze -> score.

Idempotent: reads runs.status and runs only the phases that haven't completed.

State machine:
    (no db)        -> init_run          -> "initialized"
    initialized    -> inventory_repo    -> "inventoried"
    inventoried    -> analyze_repo      -> "analyzed"
    analyzed       -> score_files       -> "scored"
    scored/running/completed/stopped -> nothing to do

Every LLM stage talks to the one ``endpoint``/``model``. The API key reaches
the stages through the ``RVH_API_KEY`` environment variable, never argv.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import connect_db  # noqa: E402

# Canonical phase order. `stop_after=<phase>` returns once the run reaches that
# phase; calling bootstrap() again without it resumes from the stored status.
_PHASE_ORDER = ("initialized", "inventoried", "analyzed", "scored")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run_step(label: str, cmd: list[str], env: dict[str, str] | None = None) -> None:
    """Run a bootstrap step, raising SystemExit on failure.

    `env` is a full environment dict (use dict(os.environ, KEY=val) to extend
    the current environment rather than replace it).
    """
    log(f"[bootstrap] {label}")
    proc = subprocess.run(cmd, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"[bootstrap] {label} failed (exit {proc.returncode})")


def current_run_status(run_dir: Path) -> str | None:
    """Read runs.status from the DB, or None if no DB yet."""
    try:
        with connect_db(run_dir) as conn:
            row = conn.execute("SELECT status FROM runs LIMIT 1").fetchone()
            return row["status"] if row else None
    except Exception:
        return None


def bootstrap(
    repo: Path,
    run_dir: Path,
    *,
    endpoint: str,
    model: str,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    vendor_policy: str = "exclude",
    debug: bool = False,
    stop_after: str | None = None,
) -> None:
    """Run the bootstrap phases the run hasn't completed yet."""
    if stop_after is not None and stop_after not in _PHASE_ORDER:
        raise ValueError(f"stop_after={stop_after!r} not in {_PHASE_ORDER}")

    def _should_stop(current: str | None) -> bool:
        if stop_after is None or current not in _PHASE_ORDER:
            return False
        return _PHASE_ORDER.index(current) >= _PHASE_ORDER.index(stop_after)

    scripts = SCRIPT_DIR
    llm_flags = ["--endpoint", endpoint, "--model", model]
    debug_flag = ["--debug"] if debug else []
    status = current_run_status(run_dir)

    # 1. Init run
    if status is None:
        cmd = [
            sys.executable, str(scripts / "init_run.py"),
            "--repo", str(repo), "--run-dir", str(run_dir),
            "--vendor-policy", vendor_policy,
        ]
        for path in include_paths or []:
            cmd += ["--include-path", path]
        for path in exclude_paths or []:
            cmd += ["--exclude-path", path]
        for glob in exclude_globs or []:
            cmd += ["--exclude-glob", glob]
        run_step("init_run", cmd)
        status = "initialized"
    if _should_stop(status):
        log(f"[bootstrap] status: {status} (stop_after={stop_after})")
        return

    # 2. Inventory
    if status == "initialized":
        run_step("inventory_repo", [sys.executable, str(scripts / "inventory_repo.py"), "--run", str(run_dir)])
        status = "inventoried"
    if _should_stop(status):
        log(f"[bootstrap] status: {status} (stop_after={stop_after})")
        return

    # 3. Analyze (classify paths by role), then derive the repo profile (no LLM).
    # The profile lives inside the 'analyzed' phase so stop_after='analyzed'
    # still lands on a populated profile (the scorer needs cold_path_prefixes).
    if status == "inventoried":
        run_step("analyze_repo", [
            sys.executable, str(scripts / "analyze_repo.py"), "--run", str(run_dir),
            *llm_flags, *debug_flag,
        ])
        run_step("derive_repo_profile", [
            sys.executable, str(scripts / "derive_repo_profile.py"), "--run", str(run_dir),
        ])
        status = "analyzed"
    if _should_stop(status):
        log(f"[bootstrap] status: {status} (stop_after={stop_after})")
        return

    # 4. Score files
    if status in ("inventoried", "analyzed"):
        run_step("score_files", [
            sys.executable, str(scripts / "score_files.py"), "--run", str(run_dir),
            *llm_flags, *debug_flag,
        ])
        status = "scored"
    if _should_stop(status):
        log(f"[bootstrap] status: {status} (stop_after={stop_after})")
        return

    log(f"[bootstrap] status: {status}")
