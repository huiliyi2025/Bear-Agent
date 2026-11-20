"""Schema migrations.

The database is the durable source of truth, so schema changes are versioned and
applied in order. Phase 1 introduces the kernel tables; later phases add task,
checkpoint, memory and claim tables without rewriting these.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 4


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'idle', 'archived')),
    model TEXT,
    provider TEXT,
    last_seq INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_epoch INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd_updated
    ON sessions (cwd, updated_at DESC);

CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    run_id TEXT,
    turn_id TEXT,
    span_id TEXT,
    parent_span_id TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content_json TEXT NOT NULL,
    token_estimate INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_session_seq
    ON messages (session_id, seq);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id TEXT,
    name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'completed', 'failed', 'unknown', 'cancelled')
    ),
    idempotent INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    result_summary TEXT,
    error_kind TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session
    ON tool_calls (session_id, started_at DESC);

CREATE TABLE IF NOT EXISTS model_calls (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_calls_session
    ON model_calls (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT,
    detail_json TEXT,
    prev_hash TEXT,
    hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_log_id ON audit_log (id);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    project_id TEXT,
    fingerprint TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('once', 'session', 'project')),
    decision TEXT NOT NULL CHECK (decision IN ('allow', 'deny')),
    reason TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_lookup
    ON approvals (fingerprint, session_id, project_id, expires_at);

CREATE TABLE IF NOT EXISTS file_claims (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    lease_owner TEXT NOT NULL,
    path TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('file', 'tree')),
    mode TEXT NOT NULL CHECK (mode IN ('exclusive', 'shared_read')),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE (session_id, path)
);
CREATE INDEX IF NOT EXISTS idx_file_claims_path ON file_claims (path);
CREATE INDEX IF NOT EXISTS idx_file_claims_session ON file_claims (session_id);

CREATE TABLE IF NOT EXISTS secret_names (
    name TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id TEXT,
    kind TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    message_seq INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_session
    ON checkpoints (session_id, event_seq DESC);

CREATE TABLE IF NOT EXISTS compactions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    level INTEGER NOT NULL CHECK (level BETWEEN 1 AND 4),
    covered_from_message_seq INTEGER NOT NULL,
    covered_to_message_seq INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    artifact_id TEXT,
    before_tokens INTEGER NOT NULL,
    after_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compactions_session
    ON compactions (session_id, covered_to_message_seq DESC);

CREATE TABLE IF NOT EXISTS context_anchors (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    source_message_seq INTEGER,
    salience REAL NOT NULL DEFAULT 1.0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anchors_session
    ON context_anchors (session_id, active);

CREATE TABLE IF NOT EXISTS context_state (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL DEFAULT 1,
    last_compaction_id TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    disabled_until_turn INTEGER,
    updated_at TEXT NOT NULL
);
"""


SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS process_records (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool_call_id TEXT,
    lease_owner TEXT NOT NULL,
    pid INTEGER NOT NULL,
    pgid INTEGER,
    command_preview TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'exited', 'terminated', 'unknown'))
);
CREATE INDEX IF NOT EXISTS idx_process_records_session
    ON process_records (session_id, status);
"""


V3_COLUMNS = (
    ("sessions", "parent_session_id", "TEXT"),
    ("sessions", "fork_message_seq", "INTEGER"),
    ("sessions", "fork_checkpoint_id", "TEXT"),
    ("artifacts", "kind", "TEXT"),
    ("artifacts", "metadata_json", "TEXT"),
)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, sql_type: str
) -> None:
    # A crash can leave a v2 database with some v3 columns already added. The
    # migration must be retryable instead of failing on "duplicate column".
    if column in _column_names(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
    version = int(row["version"]) if row else 0
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{version} is newer than this build (v{SCHEMA_VERSION})"
        )
    if version < 1:
        conn.executescript(SCHEMA_V1)
        if row:
            conn.execute("UPDATE schema_meta SET version = 1")
        else:
            conn.execute("INSERT INTO schema_meta (version) VALUES (1)")
        version = 1
    if version < 2:
        conn.executescript(SCHEMA_V2)
        conn.execute("UPDATE schema_meta SET version = 2")
        version = 2
    if version < 3:
        for table, column, sql_type in V3_COLUMNS:
            _add_column_if_missing(conn, table, column, sql_type)
        conn.executescript(SCHEMA_V3)
        conn.execute("UPDATE schema_meta SET version = 3")
        version = 3
    if version < 4:
        conn.executescript(SCHEMA_V4)
        conn.execute("UPDATE schema_meta SET version = 4")
    conn.commit()
