#!/usr/bin/env python3
"""rvh's command line.

One run per target repo, kept (and resumable) in <repo>/.rvh-runs/:

  worker image (built once per rvh version) → bootstrap (inventory →
  analyze → score), with the joern CPG built in parallel → split large files
  into regions → sandbox network + LLM gateway → engine (rolling goose-worker
  dispatch) → CANDIDATES.md
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import llm  # noqa: E402
import sandbox  # noqa: E402
from bootstrap import bootstrap as bootstrap_run  # noqa: E402
from common import DEFAULT_RUNS_DIR, SchemaVersionError, connect_db, start_caffeinate  # noqa: E402
from cpg_build import CpgBuildError, build_cpg, detect_primary_language  # noqa: E402
from cpg_sidecar import SidecarError, start as sidecar_start, stop as sidecar_stop  # noqa: E402
from image import ImageBuildError, ensure_worker_image  # noqa: E402
from preflight import PreflightError, run_preflight  # noqa: E402
from worker import log_progress  # noqa: E402

CPG_BUILD_TIMEOUT_SECONDS = 1200


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rvh",
        description="Scan a repository for security-vulnerability candidates with an LLM.",
    )
    p.add_argument("--repo", required=True, help="Target repository root")
    p.add_argument("--endpoint", required=True,
                   help="OpenAI-compatible endpoint URL, with or without a trailing /v1")
    p.add_argument("--model", required=True, help="Model name; used for every stage")
    p.add_argument("--api-key", default=None,
                   help="API key for --endpoint (default: $RVH_API_KEY, else 'dummy')")
    p.add_argument("--min-priority", type=int, default=3, choices=range(1, 6), metavar="N",
                   help="Only dispatch workers for files scored at least N (1-5, default 3)")
    p.add_argument("--parallelism", type=int, default=1, help="Concurrent workers (default 1)")
    p.add_argument("--max-tasks", type=int, default=None, help="Stop after N worker tasks")
    p.add_argument("--fresh", action="store_true",
                   help="Delete <repo>/.rvh-runs and start over (default: resume)")
    p.add_argument("--include", action="append", default=[], metavar="PATH",
                   help="Only scan under PATH (repeatable)")
    p.add_argument("--exclude", action="append", default=[], metavar="PATH_OR_GLOB",
                   help="Skip PATH, or files matching a glob containing * ? or [ (repeatable)")
    p.add_argument("--include-vendored", action="store_true",
                   help="Scan vendored code too (default: excluded)")
    p.add_argument("--no-cpg", action="store_true", help="Skip the joern code property graph")
    p.add_argument("--debug", action="store_true",
                   help="Save stage prompts and outputs under .rvh-runs/logs/debug/")
    return p


def split_excludes(values: list[str]) -> tuple[list[str], list[str]]:
    """Split --exclude values into (path prefixes, globs); a glob contains * ? or [."""
    globs = [v for v in values if any(ch in v for ch in "*?[")]
    paths = [v for v in values if v not in globs]
    return paths, globs


def phase_cpg_build_and_sidecar(
    *, repo_path: Path, run_dir: Path, lang: str, prefix: str, network: str, cpg_dir: Path | None = None,
) -> dict | None:
    """Build the CPG (into ``cpg_dir``, default <run_dir>/cpg) and start the joern
    sidecar on the run network; None (no-CPG mode) on failure."""
    try:
        result = build_cpg(
            source_root=repo_path, cpg_dir=cpg_dir or run_dir / "cpg", lang=lang,
            timeout_sec=CPG_BUILD_TIMEOUT_SECONDS,
        )
    except CpgBuildError as e:
        log_progress(f"[cpg] build failed, continuing without CPG: {e}")
        return None
    try:
        sc = sidecar_start(
            cpg_path=result["artifact"], port_file=run_dir / "sidecar.port",
            name=sandbox.cpg_name(prefix), network=network, network_alias=sandbox.CPG_ALIAS,
        )
    except SidecarError as e:
        log_progress(f"[cpg] sidecar failed to start, continuing without CPG: {e}")
        return None
    return {"endpoint": sc.endpoint, "port": sc.port, "container_id": sc.container_id}


def phase_cpg_teardown(run_dir: Path) -> None:
    """Stop the sidecar. Idempotent."""
    try:
        sidecar_stop(port_file=run_dir / "sidecar.port")
    except Exception as e:  # teardown must never mask the real exit path
        log_progress(f"[cpg] sidecar teardown warning: {e}")


def build_engine_command(
    args, run_dir: Path, image: str, network: str, cpg_enabled: bool,
) -> tuple[list[str], dict[str, str]]:
    """The engine subprocess argv, plus environment it needs on top of ours."""
    cmd = [
        # sys.executable, not a PATH-resolved "python3": the engine must run
        # under this interpreter (and venv) to see the installed dependencies.
        sys.executable, str(SCRIPT_DIR / "run_autonomous_v2.py"),
        "--repo", str(Path(args.repo).resolve()),
        "--run-dir", str(run_dir),
        "--parallelism", str(max(1, args.parallelism)),
        "--min-priority", str(args.min_priority),
        "--image", image,
        "--network", network,
        "--model", args.model,
    ]
    if args.max_tasks is not None:
        cmd += ["--max-tasks", str(max(1, args.max_tasks))]
    if args.debug:
        cmd.append("--debug")
    # Workers reach the CPG sidecar by its alias on the run network.
    env = {"RVH_CPG_ENDPOINT": sandbox.WORKER_CPG_ENDPOINT} if cpg_enabled else {}
    return cmd, env


def print_shutdown_summary(run_dir: Path) -> None:
    log_progress("")
    log_progress("=" * 60)
    try:
        with connect_db(run_dir) as conn:
            status = conn.execute("SELECT status FROM runs").fetchone()["status"]
            tasks = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")}
            findings = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
        log_progress(f"[rvh] run {status}")
        log_progress(f"  Tasks:       {tasks.get('completed', 0)}/{sum(tasks.values())} completed")
        log_progress(f"  Findings:    {findings} candidate(s) for review")
    except Exception as exc:
        log_progress(f"[rvh] could not read run state: {exc}")
    try:
        from build_candidates_index import build_index  # noqa: PLC0415
        out_path, n = build_index(run_dir)
        log_progress(f"  Candidates:  {out_path} ({n} report(s))")
    except Exception as exc:
        log_progress(f"  Candidates:  index not written: {exc}")
    log_progress(f"  Run dir:     {run_dir}")
    log_progress("=" * 60)


def main() -> None:
    start_caffeinate()
    args = build_parser().parse_args()
    args.endpoint = llm.normalize_endpoint(args.endpoint)
    args.api_key = args.api_key or os.environ.get("RVH_API_KEY") or "dummy"
    # Child stages read the key from the environment, never from argv.
    os.environ["RVH_API_KEY"] = args.api_key

    repo_root = Path(args.repo).expanduser().resolve()
    if not repo_root.is_dir():
        raise SystemExit(f"--repo {args.repo!r} is not a directory")
    run_dir = repo_root / DEFAULT_RUNS_DIR

    try:
        run_preflight(args.endpoint, args.model, args.api_key, warn=log_progress)
        image = ensure_worker_image(log_progress)
    except (PreflightError, ImageBuildError) as exc:
        raise SystemExit(f"[preflight] {exc}")

    if args.fresh and run_dir.exists():
        log_progress("[rvh] --fresh: clearing existing run state")
        shutil.rmtree(run_dir)
    if (run_dir / "state.db").exists():
        try:
            with connect_db(run_dir):
                pass
        except SchemaVersionError as exc:
            raise SystemExit(f"[rvh] {exc}")

    log_progress("=" * 60)
    log_progress(f"[rvh] {repo_root}")
    log_progress(f"  Model:       {args.model} @ {args.endpoint}")
    log_progress(f"  Parallelism: {args.parallelism}   Min priority: {args.min_priority}")
    log_progress("  Stop:        Ctrl+C (graceful), twice to force")
    log_progress("=" * 60)

    shutdown_requested = False

    def handle_sigint(signum, frame):
        nonlocal shutdown_requested
        if shutdown_requested:
            log_progress("\n[rvh] forced shutdown")
            raise SystemExit(1)
        shutdown_requested = True
        log_progress("\n[rvh] stopping: in-flight workers are requeued (Ctrl+C again to force)")

    signal.signal(signal.SIGINT, handle_sigint)

    exclude_paths, exclude_globs = split_excludes(args.exclude)
    bootstrap_kwargs = dict(
        endpoint=args.endpoint, model=args.model,
        include_paths=args.include, exclude_paths=exclude_paths, exclude_globs=exclude_globs,
        vendor_policy="include" if args.include_vendored else "exclude",
        debug=args.debug,
    )
    prefix = sandbox.run_prefix(repo_root)
    try:
        # The CPG build needs only the inventory, so it runs in a background
        # thread while analyze and score finish in the foreground.
        bootstrap_run(repo_root, run_dir, stop_after="inventoried", **bootstrap_kwargs)
        leftovers = sandbox.reclaim(prefix)
        if leftovers:
            log_progress(f"[sandbox] removed {len(leftovers)} container(s) left by an earlier run")
        network = sandbox.create_network(prefix)
        cpg_box: dict = {}
        cpg_thread = None
        if not args.no_cpg:
            lang = detect_primary_language(run_dir)
            log_progress(f"[cpg] language detected: {lang}")

            def _build_cpg() -> None:
                try:
                    cpg_box["state"] = phase_cpg_build_and_sidecar(
                        repo_path=repo_root, run_dir=run_dir, lang=lang, prefix=prefix, network=network,
                    )
                except Exception as exc:  # CPG failures must never block dispatch
                    log_progress(f"[cpg] build raised {exc!r}; continuing without CPG")

            cpg_thread = threading.Thread(target=_build_cpg, name="cpg-build", daemon=True)
            cpg_thread.start()

        bootstrap_run(repo_root, run_dir, **bootstrap_kwargs)

        cpg_state = None
        if cpg_thread is not None:
            cpg_thread.join(timeout=CPG_BUILD_TIMEOUT_SECONDS + 60)
            if cpg_thread.is_alive():
                log_progress("[cpg] build still running after bootstrap; continuing without CPG")
            else:
                cpg_state = cpg_box.get("state")

        if cpg_state is not None:
            # Split large high-priority files into region sub-tasks at function
            # boundaries (from CPG entrypoints). Best-effort.
            try:
                from decompose import expand_run  # noqa: PLC0415
                dc = expand_run(run_dir, cpg_state["endpoint"])
                if dc["files"]:
                    log_progress(f"[decompose] {dc['files']} file(s) → {dc['slices']} region sub-task(s)")
            except Exception as exc:  # noqa: BLE001
                log_progress(f"[decompose] skipped ({exc.__class__.__name__}: {exc}); whole-file dispatch")

        sandbox.start_gateway(
            prefix, image=image, endpoint=args.endpoint, api_key=args.api_key, network=network,
        )
        cmd, env = build_engine_command(args, run_dir, image, network, cpg_state is not None)
        proc = subprocess.run(cmd, env=dict(os.environ, **env))
        if proc.returncode != 0 and not shutdown_requested:
            raise SystemExit(proc.returncode)
        print_shutdown_summary(run_dir)
    except sandbox.SandboxError as exc:
        raise SystemExit(f"[sandbox] {exc}")
    finally:
        phase_cpg_teardown(run_dir)
        sandbox.reclaim(prefix)


if __name__ == "__main__":
    main()
