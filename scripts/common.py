#!/usr/bin/env python3

from __future__ import annotations

import argparse
import atexit
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


_CAFFEINATE_PROC: subprocess.Popen | None = None


def start_caffeinate(flags: str = "imsu") -> None:
    """Block macOS idle/disk/system sleep for the duration of the parent process.

    Long autonomous runs get interrupted by macOS power management when the
    system thinks no one is using it. This spawns `caffeinate` as a child and
    reaps it on exit (atexit fires on normal exit and Ctrl-C). No-op on
    non-darwin systems and silent on missing-binary.

    flags: caffeinate single-char options (default "imsu" — idle, disk,
    system, user-activity). Add "d" to also block display sleep when you
    want the screen to stay on.
    """
    global _CAFFEINATE_PROC
    if sys.platform != "darwin" or _CAFFEINATE_PROC is not None:
        return
    try:
        _CAFFEINATE_PROC = subprocess.Popen(
            ["caffeinate", f"-{flags}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        atexit.register(stop_caffeinate)
    except FileNotFoundError:
        pass


def stop_caffeinate() -> None:
    global _CAFFEINATE_PROC
    if _CAFFEINATE_PROC is not None and _CAFFEINATE_PROC.poll() is None:
        _CAFFEINATE_PROC.terminate()
        _CAFFEINATE_PROC = None


DEFAULT_RUNS_DIR = ".rvh-runs"
DEFAULT_LEASE_SECONDS = 1800
DEFAULT_HEARTBEAT_SECONDS = 120
DEFAULT_MAX_ATTEMPTS = 3

TASK_PENDING = "pending"
TASK_RESERVED = "reserved"
TASK_IN_PROGRESS = "in_progress"
TASK_COMPLETED = "completed"
TASK_NEEDS_FOLLOWUP = "needs_followup"
TASK_REJECTED = "rejected"
TASK_FAILED = "failed"
TASK_REQUEUED = "requeued"


# Bump when the schema changes. A run made under another version is refused
# (re-run with --fresh) rather than migrated.
SCHEMA_VERSION = 2


class SchemaVersionError(RuntimeError):
    """state.db was written by an rvh version with a different schema."""


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    label TEXT,
    repo_root TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    rel_dir TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    is_binary INTEGER NOT NULL,
    category TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    excluded INTEGER NOT NULL DEFAULT 0,
    exclusion_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    file_id INTEGER PRIMARY KEY,
    score INTEGER NOT NULL,
    rationale TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    scored_by TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    priority INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    owner TEXT,
    owner_session TEXT,
    notes TEXT,
    reserved_at TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    completed_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    anchor_start_line INTEGER,
    anchor_end_line INTEGER,
    parent_task_id INTEGER REFERENCES tasks(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS worker_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    report_path TEXT NOT NULL,
    title TEXT NOT NULL,
    verdict TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    report_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    finding_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence TEXT NOT NULL,
    status TEXT NOT NULL,
    anchor_file TEXT NOT NULL,
    related_files_json TEXT NOT NULL,
    fingerprint TEXT,
    summary TEXT NOT NULL,
    dynamic_evidence INTEGER NOT NULL DEFAULT 0,
    cwe TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(report_id) REFERENCES worker_reports(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority DESC, id ASC);
CREATE INDEX IF NOT EXISTS idx_findings_status_severity ON findings(status, severity, confidence);
CREATE INDEX IF NOT EXISTS idx_findings_fingerprint ON findings(fingerprint);
"""


REPO_PROFILE_LIST_KEYS = {
    "languages",
    "framework_hints",
    "entrypoint_paths",
    "hot_paths",
    "hot_path_prefixes",
    "cold_paths",
    "cold_path_prefixes",
}
REPO_PROFILE_DICT_KEYS = {"keyword_boosts"}
REPO_PROFILE_SCALAR_KEYS = {"schema_version", "source", "notes", "created_at", "updated_at"}
REPO_PROFILE_ALLOWED_KEYS = REPO_PROFILE_LIST_KEYS | REPO_PROFILE_DICT_KEYS | REPO_PROFILE_SCALAR_KEYS


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def add_seconds(iso_timestamp: str, seconds: int) -> str:
    base = datetime.fromisoformat(iso_timestamp)
    return datetime.fromtimestamp(base.timestamp() + seconds, tz=timezone.utc).replace(microsecond=0).isoformat()


def repo_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def run_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def ensure_run_dirs(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "reports").mkdir(exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)


def db_path(run_dir: Path) -> Path:
    return run_dir / "state.db"


@contextmanager
def connect_db(run_dir: Path):
    conn = sqlite3.connect(db_path(run_dir))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    ensure_schema(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(run_dir: Path) -> None:
    ensure_run_dirs(run_dir)
    with connect_db(run_dir) as conn:
        pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the schema on a new DB; refuse a DB from another schema version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_runs = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
    ).fetchone() is not None
    if has_runs and version != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"{DEFAULT_RUNS_DIR}/state.db uses schema v{version} but this rvh version "
            f"needs v{SCHEMA_VERSION}; it was made by an older version — re-run with --fresh"
        )
    conn.executescript(SCHEMA)
    if not has_runs:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def count_tokens(text: str) -> int:
    """Approximate token count (chars / 3.5); sizes decomposition slices."""
    return int(len(text) / 3.5)


def write_config(run_dir: Path, data: dict) -> None:
    config_path = run_dir / "config.json"
    config_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_config(run_dir: Path) -> dict:
    config_path = run_dir / "config.json"
    return json.loads(config_path.read_text())


def repo_profile_path(run_dir: Path) -> Path:
    return run_dir / "repo_profile.json"


def default_repo_profile(*, source: str = "bootstrap") -> dict:
    now = utc_now()
    return {
        "schema_version": 1,
        "source": source,
        "languages": [],
        "framework_hints": [],
        "entrypoint_paths": [],
        "hot_paths": [],
        "hot_path_prefixes": [],
        "cold_paths": [],
        "cold_path_prefixes": [],
        "keyword_boosts": {},
        "notes": "",
        "created_at": now,
        "updated_at": now,
    }


def _validate_string_list(key: str, value) -> list[str]:
    if not isinstance(value, list):
        raise SystemExit(f"repo profile field {key!r} must be a list of strings")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SystemExit(f"repo profile field {key!r} must contain only non-empty strings")
        cleaned.append(item.strip())
    return sorted(dict.fromkeys(cleaned))


def _validate_keyword_boosts(value) -> dict[str, int]:
    if not isinstance(value, dict):
        raise SystemExit("repo profile field 'keyword_boosts' must be an object mapping keywords to integer boosts")
    cleaned: dict[str, int] = {}
    for raw_key, raw_weight in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise SystemExit("repo profile keyword_boosts keys must be non-empty strings")
        if not isinstance(raw_weight, int):
            raise SystemExit("repo profile keyword_boosts values must be integers")
        cleaned[raw_key.strip().lower()] = max(-5, min(5, raw_weight))
    return dict(sorted(cleaned.items()))


def normalize_repo_profile(
    data: dict,
    *,
    source_override: str | None = None,
    preserve_created_at: str | None = None,
    preserve_updated_at: str | None = None,
    touch_updated_at: bool = True,
) -> dict:
    if not isinstance(data, dict):
        raise SystemExit("repo profile must be a JSON object")
    unexpected = set(data) - REPO_PROFILE_ALLOWED_KEYS
    if unexpected:
        raise SystemExit(f"repo profile has unexpected fields: {', '.join(sorted(unexpected))}")
    normalized = default_repo_profile(source=source_override or str(data.get("source") or "manual"))
    if preserve_created_at:
        normalized["created_at"] = preserve_created_at
    elif isinstance(data.get("created_at"), str) and data["created_at"].strip():
        normalized["created_at"] = data["created_at"].strip()

    for key in REPO_PROFILE_LIST_KEYS:
        if key in data:
            normalized[key] = _validate_string_list(key, data[key])
    if "keyword_boosts" in data:
        normalized["keyword_boosts"] = _validate_keyword_boosts(data["keyword_boosts"])
    if "schema_version" in data:
        if not isinstance(data["schema_version"], int) or data["schema_version"] <= 0:
            raise SystemExit("repo profile field 'schema_version' must be a positive integer")
        normalized["schema_version"] = data["schema_version"]
    if "source" in data:
        if not isinstance(data["source"], str) or not data["source"].strip():
            raise SystemExit("repo profile field 'source' must be a non-empty string")
        normalized["source"] = data["source"].strip()
    if source_override:
        normalized["source"] = source_override
    if "notes" in data:
        if not isinstance(data["notes"], str):
            raise SystemExit("repo profile field 'notes' must be a string")
        normalized["notes"] = data["notes"].strip()
    if preserve_updated_at:
        normalized["updated_at"] = preserve_updated_at
    elif isinstance(data.get("updated_at"), str) and data["updated_at"].strip():
        normalized["updated_at"] = data["updated_at"].strip()
    if touch_updated_at:
        normalized["updated_at"] = utc_now()
    return normalized


def load_repo_profile(run_dir: Path) -> dict:
    path = repo_profile_path(run_dir)
    if not path.exists():
        profile = default_repo_profile()
        write_repo_profile(run_dir, profile)
        return profile
    raw = json.loads(path.read_text())
    return normalize_repo_profile(
        raw,
        preserve_created_at=str(raw.get("created_at") or utc_now()),
        preserve_updated_at=str(raw.get("updated_at") or utc_now()),
        touch_updated_at=False,
    )


def load_repo_analysis(run_dir: Path) -> dict | None:
    """Load repo_analysis.json from the run directory, or return None if missing."""
    path = Path(run_dir) / "repo_analysis.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def save_repo_analysis(run_dir: Path, analysis: dict) -> None:
    """Write repo_analysis.json to the run directory."""
    path = Path(run_dir) / "repo_analysis.json"
    with open(path, "w") as f:
        json.dump(analysis, f, indent=2)
        f.write("\n")


def write_repo_profile(run_dir: Path, data: dict) -> None:
    path = repo_profile_path(run_dir)
    normalized = normalize_repo_profile(
        data,
        preserve_created_at=str(data.get("created_at") or utc_now()),
        preserve_updated_at=str(data.get("updated_at") or utc_now()),
    )
    path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")


def merge_repo_profile(existing: dict, patch: dict, *, source_override: str | None = None) -> dict:
    merged = dict(existing)
    for key in REPO_PROFILE_LIST_KEYS:
        if key in patch:
            merged[key] = sorted(dict.fromkeys(list(existing.get(key, [])) + _validate_string_list(key, patch[key])))
    if "keyword_boosts" in patch:
        keyword_boosts = dict(existing.get("keyword_boosts", {}))
        keyword_boosts.update(_validate_keyword_boosts(patch["keyword_boosts"]))
        merged["keyword_boosts"] = dict(sorted(keyword_boosts.items()))
    for key in ("schema_version", "source", "notes"):
        if key in patch:
            merged[key] = patch[key]
    merged["created_at"] = str(existing.get("created_at") or utc_now())
    merged["updated_at"] = str(existing.get("updated_at") or utc_now())
    return normalize_repo_profile(merged, source_override=source_override, preserve_created_at=merged["created_at"])


def append_event(run_dir: Path, event: dict) -> None:
    path = run_dir / "logs" / "events.jsonl"
    payload = dict(event)
    payload.setdefault("at", utc_now())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def update_run_timestamp(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (utc_now(), run_id))


def require_run(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs LIMIT 1").fetchone()
    if row is None:
        raise SystemExit("run metadata missing from database")
    return row


def parse_common_run_arg() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, help="Path to the .rvh-runs directory")
    return parser


def normalize_rel_path(path: Path, repo_root: Path) -> str:
    return path.resolve().relative_to(repo_root).as_posix()


def walk_repo(repo_root: Path):
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = sorted(d for d in dirs if d not in {".git", ".hg", ".svn", ".rvh-runs", "node_modules", ".pytest_cache", "__pycache__"})
        for name in sorted(files):
            yield Path(root) / name


def task_with_file(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT tasks.*, files.path, files.size_bytes, scores.score AS score, scores.rationale AS score_rationale
        FROM tasks
        JOIN files ON files.id = tasks.file_id
        LEFT JOIN scores ON scores.file_id = files.id
        WHERE tasks.id = ?
        """,
        (task_id,),
    ).fetchone()


def require_task_owner(row: sqlite3.Row, owner: str, owner_session: str | None) -> None:
    if row["owner"] != owner:
        raise SystemExit(f"task {row['id']} is owned by {row['owner']!r}, not {owner!r}")
    if owner_session is not None and row["owner_session"] != owner_session:
        raise SystemExit(
            f"task {row['id']} is bound to owner_session {row['owner_session']!r}, not {owner_session!r}"
        )


def debug_log(run_dir: Path, category: str, label: str, content: str) -> None:
    """Write debug content to logs/debug/<category>-<label>.txt"""
    debug_dir = Path(run_dir) / "logs" / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"{category}-{label}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def extract_last_assistant_text(stdout_path: Path) -> str | None:
    """Pull the last assistant `text` block from a goose stdout JSON log.

    Goose emits a 3-line CLI banner before the JSON object, so we anchor
    the parse on the first `{`. Returns None when the file is missing,
    unparseable, contains no assistant text, or the trailing assistant
    text is empty/whitespace.

    Used by the autonomous runner to salvage a finished investigation when
    the worker forgot to call `write` — the model has already produced a
    final analysis as inline text and we feed it to a tight follow-up
    prompt rather than redoing the work from scratch.
    """
    try:
        raw = Path(stdout_path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    start = raw.find("{")
    if start < 0:
        return None
    try:
        data = json.loads(raw[start:])
    except json.JSONDecodeError:
        return None
    last_text: str | None = None
    for msg in data.get("messages", []) or []:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []) or []:
            if block.get("type") == "text":
                txt = block.get("text") or ""
                if txt.strip():
                    last_text = txt
    return last_text
