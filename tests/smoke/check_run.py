#!/usr/bin/env python3
"""Summarize — and with --assert, gate — a rvh smoke run.

    .venv/bin/python tests/smoke/check_run.py --repo ~/projects/osg/scitokens-cpp [--assert]

Prints a JSON summary. With --assert, exits 1 listing every failed criterion:
  * run status is completed or stopped
  * >= 2 tasks completed, >= 2 worker reports ingested
  * >= 1 region (decomposed) task exists
  * CANDIDATES.md exists
  * teardown left nothing behind: no rvh-<repo>* containers or networks,
    no joern container, no sidecar/proxy state files, nothing on :8898
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path


def _slug(repo: Path) -> str:
    return re.sub(r"[^a-z0-9_.-]", "-", repo.name.lower())


def _docker_lines(*args: str) -> list[str]:
    out = subprocess.run(["docker", *args], capture_output=True, text=True, check=False).stdout
    return [line for line in out.splitlines() if line.strip()]


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def summarize(repo: Path) -> dict:
    run_dir = repo / ".rvh-runs"
    conn = sqlite3.connect(run_dir / "state.db")
    conn.row_factory = sqlite3.Row

    def one(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    prefix = f"rvh-{_slug(repo)}"
    return {
        "run_status": conn.execute("SELECT status FROM runs").fetchone()[0],
        "tasks": {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")},
        "region_tasks": one("SELECT COUNT(*) FROM tasks WHERE anchor_start_line IS NOT NULL"),
        "worker_reports": one("SELECT COUNT(*) FROM worker_reports"),
        "findings": one("SELECT COUNT(*) FROM findings"),
        "candidates_md": (run_dir / "CANDIDATES.md").exists(),
        "leftover_containers": [n for n in _docker_lines("ps", "-a", "--format", "{{.Names}}") if n.startswith(prefix)],
        "leftover_networks": [n for n in _docker_lines("network", "ls", "--format", "{{.Name}}") if n.startswith(prefix)],
        "joern_containers": [l for l in _docker_lines("ps", "--format", "{{.Image}} {{.Names}}") if "joernio/joern" in l],
        "state_files_left": sorted(
            n for n in ("sidecar.port", "sidecar.container-id")
            if (run_dir / n).exists()
        ),
        "port_8898_listening": _listening(8898),
    }


def failures(s: dict) -> list[str]:
    bad = []
    if s["run_status"] not in ("completed", "stopped"):
        bad.append(f"run_status={s['run_status']}")
    if s["tasks"].get("completed", 0) < 2:
        bad.append(f"completed tasks={s['tasks'].get('completed', 0)} (<2)")
    if s["worker_reports"] < 2:
        bad.append(f"worker_reports={s['worker_reports']} (<2)")
    if s["region_tasks"] < 1:
        bad.append("no region tasks (decomposition did not run)")
    if not s["candidates_md"]:
        bad.append("CANDIDATES.md missing")
    for key in ("leftover_containers", "leftover_networks", "joern_containers", "state_files_left"):
        if s[key]:
            bad.append(f"{key}={s[key]}")
    if s["port_8898_listening"]:
        bad.append("something still listening on :8898")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--assert", dest="check", action="store_true")
    args = ap.parse_args()
    summary = summarize(args.repo.expanduser().resolve())
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.check:
        bad = failures(summary)
        if bad:
            print("SMOKE FAIL:\n  " + "\n  ".join(bad), file=sys.stderr)
            raise SystemExit(1)
        print("SMOKE PASS", file=sys.stderr)


if __name__ == "__main__":
    main()
