#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    connect_db,
    default_repo_profile,
    init_db,
    repo_path,
    utc_now,
    write_config,
    write_repo_profile,
)


def normalize_repo_rel_path(repo_root: Path, raw_value: str) -> str:
    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (repo_root / candidate).resolve()
    try:
        rel = resolved.relative_to(repo_root).as_posix()
        return "" if rel == "." else rel
    except ValueError as exc:
        raise SystemExit(f"scope path {raw_value!r} is outside the repository root") from exc


def normalize_scope_paths(repo_root: Path, values: list[str]) -> list[str]:
    return sorted({path for path in (normalize_repo_rel_path(repo_root, value) for value in values if value.strip()) if path})


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize a repo-vuln-hunt run")
    parser.add_argument("--repo", default=".", help="Repository root")
    parser.add_argument("--run-dir", required=True, help="Run directory")
    parser.add_argument("--parallelism", type=int, default=8, help="Suggested worker pool size")
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS, help="Task lease duration")
    parser.add_argument("--heartbeat-seconds", type=int, default=DEFAULT_HEARTBEAT_SECONDS, help="Expected heartbeat interval")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS, help="Max reserve attempts before failure")
    parser.add_argument("--include-path", action="append", default=[], help="Optional scoped include path, repeatable")
    parser.add_argument("--exclude-path", action="append", default=[], help="Optional scoped exclude path, repeatable")
    parser.add_argument("--exclude-glob", action="append", default=[], help="Optional repo-relative glob to exclude, repeatable")
    parser.add_argument(
        "--vendor-policy",
        choices=("include", "deprioritize", "exclude"),
        default="exclude",
        help="How to treat vendored code",
    )
    parser.add_argument(
        "--generated-policy",
        choices=("include", "deprioritize", "exclude"),
        default="deprioritize",
        help="How to treat generated code",
    )
    parser.add_argument(
        "--test-policy",
        choices=("include", "deprioritize", "exclude"),
        default="deprioritize",
        help="How to treat tests, fixtures, and examples",
    )
    parser.add_argument("--max-file-size", type=int, default=262144, help="Exclude files larger than this many bytes; 0 disables")
    args = parser.parse_args()

    repo_root = repo_path(args.repo)
    run_dir = Path(args.run_dir).expanduser().resolve()
    run_id = run_dir.name

    init_db(run_dir)

    include_paths = normalize_scope_paths(repo_root, args.include_path)
    exclude_paths = normalize_scope_paths(repo_root, args.exclude_path)
    exclude_globs = sorted({item.strip() for item in args.exclude_glob if item.strip()})
    max_file_size = max(0, args.max_file_size)

    config = {
        "run_id": run_id,
        "repo_root": str(repo_root),
        "parallelism": max(1, args.parallelism),
        "lease_seconds": max(60, args.lease_seconds),
        "heartbeat_seconds": max(15, args.heartbeat_seconds),
        "max_attempts": max(1, args.max_attempts),
        "include_paths": include_paths,
        "exclude_paths": exclude_paths,
        "exclude_globs": exclude_globs,
        "vendor_policy": args.vendor_policy,
        "generated_policy": args.generated_policy,
        "test_policy": args.test_policy,
        "max_file_size": max_file_size,
        "worker_contract": {
            "task_states": [
                "pending",
                "reserved",
                "in_progress",
                "completed",
                "needs_followup",
                "rejected",
                "failed",
                "requeued",
            ],
            "report_validation_mode": "parent_only",
            "report_format": "markdown_frontmatter",
        },
        "created_at": utc_now(),
        "status": "initialized",
    }
    write_config(run_dir, config)
    write_repo_profile(run_dir, default_repo_profile(source="bootstrap"))
    (run_dir / "logs" / "dispatcher.log").write_text("")
    (run_dir / "logs" / "events.jsonl").write_text(json.dumps({"event": "run_initialized", "at": config["created_at"]}) + "\n")

    with connect_db(run_dir) as conn:
        conn.execute(
            """
            INSERT INTO runs (id, label, repo_root, created_at, updated_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, run_id, str(repo_root), config["created_at"], config["created_at"], "initialized"),
        )

    print(run_dir)


if __name__ == "__main__":
    main()
