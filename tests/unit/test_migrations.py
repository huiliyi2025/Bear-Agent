"""Schema migrations from an existing v1/v2 database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bear_agent.storage.database import Database
from bear_agent.storage.migrations import SCHEMA_V1, SCHEMA_V2, SCHEMA_VERSION


def _make_v1(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_V1)
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_meta (version) VALUES (1)")
        conn.commit()
    finally:
        conn.close()


def _make_v2(path: Path) -> None:
    _make_v1(path)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_V2)
        conn.execute("UPDATE schema_meta SET version = 2")
        conn.commit()
    finally:
        conn.close()


def _columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT version FROM schema_meta").fetchone()[0])
    finally:
        conn.close()


def test_v1_database_migrates_to_current(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    _make_v1(path)
    db = Database(path)
    try:
        assert _version(path) == SCHEMA_VERSION
        assert {"parent_session_id", "fork_message_seq", "fork_checkpoint_id"} <= _columns(
            path, "sessions"
        )
        assert {"kind", "metadata_json"} <= _columns(path, "artifacts")
        for table in (
            "checkpoints",
            "compactions",
            "context_anchors",
            "context_state",
            "process_records",
            "plans",
            "plan_revisions",
            "tasks",
            "task_dependencies",
            "task_attempts",
            "traces",
            "spans",
            "benchmark_runs",
        ):
            assert _columns(path, table), table
    finally:
        db.close()


def test_v2_database_migrates_to_current(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    _make_v2(path)
    db = Database(path)
    try:
        assert _version(path) == SCHEMA_VERSION
        assert _columns(path, "process_records")
        assert _columns(path, "checkpoints")
        assert _columns(path, "plans")
        assert _columns(path, "tasks")
    finally:
        db.close()


def test_partial_v3_column_addition_is_retryable(tmp_path: Path) -> None:
    path = tmp_path / "partial.db"
    _make_v2(path)
    conn = sqlite3.connect(path)
    try:
        # Simulate a crash after the first ALTER but before the version bump.
        conn.execute("ALTER TABLE sessions ADD COLUMN parent_session_id TEXT")
        conn.commit()
    finally:
        conn.close()
    db = Database(path)
    try:
        assert _version(path) == SCHEMA_VERSION
        assert {"parent_session_id", "fork_message_seq", "fork_checkpoint_id"} <= _columns(
            path, "sessions"
        )
        assert {"kind", "metadata_json"} <= _columns(path, "artifacts")
    finally:
        db.close()


def test_newer_database_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION + 1,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="newer than this build"):
        Database(path)
