"""state.db carries a schema version; a run made by an older rvh must be re-run with --fresh."""
import sqlite3

import pytest

from common import SCHEMA_VERSION, SchemaVersionError, connect_db, init_db


def test_new_db_is_stamped_with_the_current_version(tmp_path):
    init_db(tmp_path)
    conn = sqlite3.connect(tmp_path / "state.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_reopening_a_current_db_works(tmp_path):
    init_db(tmp_path)
    with connect_db(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_pre_refactor_db_is_rejected_with_a_fresh_hint(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")  # an old run: tables, no version stamp
    conn.commit()
    conn.close()
    with pytest.raises(SchemaVersionError, match="--fresh"):
        with connect_db(tmp_path):
            pass


def test_validation_era_columns_are_gone(tmp_path):
    init_db(tmp_path)
    with connect_db(tmp_path) as conn:
        finding_cols = {r["name"] for r in conn.execute("PRAGMA table_info(findings)")}
        report_cols = {r["name"] for r in conn.execute("PRAGMA table_info(worker_reports)")}
    assert {"duplicate_of", "validator_notes", "dynamic_validation_status", "source"}.isdisjoint(finding_cols)
    assert {"validated", "lint_tier", "refuter_status"}.isdisjoint(report_cols)
