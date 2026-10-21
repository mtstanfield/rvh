"""Seed an eval run dir: a state.db whose only tasks are a suite's anchors.

This replaces inventory, analyze, and scoring for an eval; the engine then
dispatches the seeded tasks exactly as it would a scan's.
"""
from __future__ import annotations

import json
from pathlib import Path

from common import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    TASK_PENDING,
    connect_db,
    default_repo_profile,
    init_db,
    utc_now,
    write_config,
    write_repo_profile,
)
from eval_corpus import Anchor, Repo

SEED_FILE = "seed.json"


def seed_run(run_dir: Path, *, repo: Repo, checkout: Path, anchors: list[Anchor], repeats: int) -> dict:
    """Create ``run_dir`` with one pending task per anchor × repeat; return the seed map.

    Tasks go in repeat-major order (every anchor's repeat 0, then repeat 1, …)
    so an interrupted eval has whole repeats finished first. seed.json maps
    each task id to its anchor and repeat for scoring.
    """
    if (run_dir / "state.db").exists():
        raise FileExistsError(f"{run_dir} is already seeded")
    init_db(run_dir)
    now = utc_now()
    write_config(run_dir, {
        "run_id": run_dir.name,
        "repo_root": str(checkout),
        "lease_seconds": DEFAULT_LEASE_SECONDS,
        "heartbeat_seconds": DEFAULT_HEARTBEAT_SECONDS,
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "created_at": now,
        "status": "scored",
    })
    # analyze_repo is skipped, so the frozen hints stand in for its framework detection.
    write_repo_profile(run_dir, {**default_repo_profile(source="eval"), "framework_hints": list(repo.framework_hints)})

    tasks: dict[str, dict] = {}
    with connect_db(run_dir) as conn:
        conn.execute(
            "INSERT INTO runs (id, label, repo_root, created_at, updated_at, status) VALUES (?, ?, ?, ?, ?, 'scored')",
            (run_dir.name, f"eval:{repo.id}", str(checkout), now, now),
        )
        file_ids: dict[str, int] = {}
        for anchor in anchors:
            if anchor.path in file_ids:
                continue
            rel = Path(anchor.path)
            file_ids[anchor.path] = conn.execute(
                """INSERT INTO files (path, rel_dir, extension, size_bytes, is_binary, category,
                                      tags_json, excluded, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, 'code', '[]', 0, ?, ?)""",
                (anchor.path, "/" if str(rel.parent) == "." else rel.parent.as_posix(),
                 rel.suffix.lower(), (checkout / rel).stat().st_size, now, now),
            ).lastrowid
        for repeat in range(repeats):
            for anchor in anchors:
                start, end = anchor.region or (None, None)
                task_id = conn.execute(
                    """INSERT INTO tasks (file_id, priority, status, anchor_start_line, anchor_end_line,
                                          created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (file_ids[anchor.path], anchor.priority, TASK_PENDING, start, end, now, now),
                ).lastrowid
                tasks[str(task_id)] = {"anchor": anchor.id, "repeat": repeat}

    seed = {"repo": repo.id, "rev": repo.rev, "checkout": str(checkout), "repeats": repeats, "tasks": tasks}
    (run_dir / SEED_FILE).write_text(json.dumps(seed, indent=2, sort_keys=True) + "\n")
    return seed


def load_seed(run_dir: Path) -> dict:
    return json.loads((run_dir / SEED_FILE).read_text())
