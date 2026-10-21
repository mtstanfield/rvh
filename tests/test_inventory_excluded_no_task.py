"""Tests for the inventory-phase invariant: tasks rows are only created for
non-excluded files, and existing pending tasks for now-excluded files get
bumped to 'rejected' on re-inventory.

Regression for the coriolix run where 316 excluded files (binary, oversize,
docs, assets) sat in the tasks table as 'pending' forever, polluting the
progress denominator (199/728 looked bad; the real ratio was 199/412).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS_DIR)}
    return subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)


def _make_repo(repo_root: Path) -> None:
    """Three files: one ordinary text (kept), one binary (excluded by
    is_binary), one oversized text (excluded by max_file_size)."""
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "good.py").write_text("print('hello')\n")
    # 100 NUL bytes — detect_binary trips on the NUL.
    (repo_root / "blob.bin").write_bytes(b"\x00" * 100)
    # > max_file_size we'll set below.
    (repo_root / "huge.py").write_text("x = 1\n" * 50_000)


def _init_run(repo_root: Path, run_dir: Path, *, max_file_size: int) -> None:
    _run([
        sys.executable, str(SCRIPTS_DIR / "init_run.py"),
        "--repo", str(repo_root),
        "--run-dir", str(run_dir),
        "--max-file-size", str(max_file_size),
    ])


def _run_inventory(run_dir: Path) -> None:
    _run([sys.executable, str(SCRIPTS_DIR / "inventory_repo.py"), "--run", str(run_dir)])


def _query(db: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_excluded_files_never_get_a_task_row(tmp_path):
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    _make_repo(repo_root)
    _init_run(repo_root, run_dir, max_file_size=1024)
    _run_inventory(run_dir)

    db = run_dir / "state.db"

    files = _query(db, "SELECT path, excluded, exclusion_reason FROM files ORDER BY path")
    files_by_path = {r["path"]: r for r in files}
    assert "good.py" in files_by_path and files_by_path["good.py"]["excluded"] == 0
    assert "blob.bin" in files_by_path and files_by_path["blob.bin"]["excluded"] == 1
    assert "huge.py" in files_by_path and files_by_path["huge.py"]["excluded"] == 1

    # The whole point: only good.py should have a tasks row.
    tasks = _query(
        db,
        "SELECT f.path, t.status FROM tasks t JOIN files f ON f.id = t.file_id "
        "ORDER BY f.path",
    )
    paths_with_tasks = [t["path"] for t in tasks]
    assert paths_with_tasks == ["good.py"], (
        f"Expected only good.py to have a task; got {paths_with_tasks}"
    )


def test_re_inventory_rejects_existing_task_when_file_becomes_excluded(tmp_path):
    """If a file was OK on the first inventory pass and then becomes excluded
    on a re-run (e.g., the user lowers max_file_size, or the file grows), its
    pending task row must transition to 'rejected', not stay 'pending'."""
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    repo_root.mkdir()
    (repo_root / "growing.py").write_text("x = 1\n")  # small, kept

    # First pass: max_file_size is generous, growing.py is kept and gets a task.
    _init_run(repo_root, run_dir, max_file_size=1024)
    _run_inventory(run_dir)

    db = run_dir / "state.db"
    rows = _query(
        db,
        "SELECT t.status FROM tasks t JOIN files f ON f.id = t.file_id "
        "WHERE f.path = 'growing.py'",
    )
    assert len(rows) == 1 and rows[0]["status"] == "pending"

    # Bloat the file AND lower max_file_size in config so the same path now
    # exceeds the threshold. init_run isn't idempotent, so edit config.json
    # in place rather than re-init the run.
    (repo_root / "growing.py").write_text("x = 1\n" * 1000)
    config_path = run_dir / "config.json"
    config = json.loads(config_path.read_text())
    config["max_file_size"] = 100
    config_path.write_text(json.dumps(config))
    _run_inventory(run_dir)

    rows = _query(
        db,
        "SELECT t.status, t.notes FROM tasks t JOIN files f ON f.id = t.file_id "
        "WHERE f.path = 'growing.py'",
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected", (
        f"Expected pending→rejected on re-inventory after exclusion; "
        f"got status={rows[0]['status']!r}"
    )
    assert rows[0]["notes"], "Expected exclusion reason to be recorded in notes"
