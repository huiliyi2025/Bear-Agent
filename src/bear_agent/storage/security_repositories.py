"""Repositories for security state: approvals, file claims and secret names.

Approvals and claims are security decisions, so their reads/writes are explicit
transactions. Claims additionally consult the session lease row so a process
that lost its lease cannot keep a claim or pass an ownership check.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bear_agent.core.errors import ClaimConflict, ClaimError
from bear_agent.storage.database import Database, transaction


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: str
    fingerprint: str
    tool_name: str
    scope: str
    decision: str
    session_id: str | None
    project_id: str | None
    reason: str | None
    created_at: datetime
    expires_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ApprovalRecord:
        return cls(
            id=row["id"],
            fingerprint=row["fingerprint"],
            tool_name=row["tool_name"],
            scope=row["scope"],
            decision=row["decision"],
            session_id=row["session_id"],
            project_id=row["project_id"],
            reason=row["reason"],
            created_at=_parse_dt(row["created_at"]) or datetime.now(UTC),
            expires_at=_parse_dt(row["expires_at"]) or datetime.now(UTC),
        )


class ApprovalRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def save(self, record: ApprovalRecord) -> None:
        await self.db.run(lambda conn: self._save(conn, record))

    def _save(self, conn: sqlite3.Connection, record: ApprovalRecord) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO approvals (
                id, fingerprint, tool_name, scope, decision, session_id,
                project_id, reason, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.fingerprint,
                record.tool_name,
                record.scope,
                record.decision,
                record.session_id,
                record.project_id,
                record.reason,
                _iso(record.created_at),
                _iso(record.expires_at),
            ),
        )

    async def find(
        self,
        fingerprint: str,
        *,
        session_id: str,
        project_id: str | None,
        now: datetime,
    ) -> ApprovalRecord | None:
        return await self.db.run(
            lambda conn: self._find(conn, fingerprint, session_id, project_id, now)
        )

    def _find(
        self,
        conn: sqlite3.Connection,
        fingerprint: str,
        session_id: str,
        project_id: str | None,
        now: datetime,
    ) -> ApprovalRecord | None:
        row = conn.execute(
            """
            SELECT * FROM approvals
             WHERE fingerprint = ?
               AND expires_at > ?
               AND (
                    (session_id IS NOT NULL AND session_id = ?)
                 OR (project_id IS NOT NULL AND project_id = ?)
               )
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (fingerprint, _iso(now), session_id, project_id),
        ).fetchone()
        return ApprovalRecord.from_row(row) if row else None

    async def delete_expired(self, now: datetime) -> int:
        return await self.db.run(lambda conn: self._delete_expired(conn, now))

    def _delete_expired(self, conn: sqlite3.Connection, now: datetime) -> int:
        cursor = conn.execute("DELETE FROM approvals WHERE expires_at <= ?", (_iso(now),))
        return cursor.rowcount

    async def list_recent(self, limit: int = 50) -> list[ApprovalRecord]:
        return await self.db.run(lambda conn: self._list_recent(conn, limit))

    def _list_recent(self, conn: sqlite3.Connection, limit: int) -> list[ApprovalRecord]:
        rows = conn.execute(
            "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [ApprovalRecord.from_row(row) for row in rows]


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    id: str
    session_id: str
    lease_owner: str
    path: str
    scope: str
    mode: str
    acquired_at: datetime
    expires_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ClaimRecord:
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            lease_owner=row["lease_owner"],
            path=row["path"],
            scope=row["scope"],
            mode=row["mode"],
            acquired_at=_parse_dt(row["acquired_at"]) or datetime.now(UTC),
            expires_at=_parse_dt(row["expires_at"]) or datetime.now(UTC),
        )


def _under(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root + "/")


def _paths_conflict(a: ClaimRecord, b: ClaimRecord) -> bool:
    if a.session_id == b.session_id:
        return False
    if a.mode == "shared_read" and b.mode == "shared_read":
        return False
    if a.path == b.path:
        return True
    if a.scope == "tree" and _under(b.path, a.path):
        return True
    return b.scope == "tree" and _under(a.path, b.path)


class ClaimRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def acquire(self, record: ClaimRecord, now: datetime) -> ClaimRecord:
        return await self.db.run(lambda conn: self._acquire(conn, record, now))

    def _acquire(self, conn: sqlite3.Connection, record: ClaimRecord, now: datetime) -> ClaimRecord:
        with transaction(conn):
            self._purge_stale(conn, now)
            lease_row = conn.execute(
                "SELECT lease_owner, lease_expires_at FROM sessions WHERE id = ?",
                (record.session_id,),
            ).fetchone()
            lease_expires = _parse_dt(lease_row["lease_expires_at"]) if lease_row else None
            if (
                lease_row is None
                or lease_row["lease_owner"] != record.lease_owner
                or lease_expires is None
                or lease_expires <= now
            ):
                raise ClaimError(
                    "session lease is no longer valid for this run; refusing to acquire a claim"
                )
            rows = conn.execute("SELECT * FROM file_claims").fetchall()
            for row in rows:
                other = ClaimRecord.from_row(row)
                if _paths_conflict(record, other):
                    raise ClaimConflict(
                        f"path {record.path!r} conflicts with a claim held by "
                        f"session {other.session_id} on {other.path!r}",
                        holder=other.session_id,
                        path=other.path,
                    )
            conn.execute(
                """
                INSERT INTO file_claims (
                    id, session_id, lease_owner, path, scope, mode,
                    acquired_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, path) DO UPDATE SET
                    lease_owner = excluded.lease_owner,
                    scope = excluded.scope,
                    mode = excluded.mode,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                """,
                (
                    record.id,
                    record.session_id,
                    record.lease_owner,
                    record.path,
                    record.scope,
                    record.mode,
                    _iso(record.acquired_at),
                    _iso(record.expires_at),
                ),
            )
            return record

    def _purge_stale(self, conn: sqlite3.Connection, now: datetime) -> None:
        conn.execute("DELETE FROM file_claims WHERE expires_at <= ?", (_iso(now),))
        conn.execute(
            """
            DELETE FROM file_claims
             WHERE NOT EXISTS (
                SELECT 1 FROM sessions s
                 WHERE s.id = file_claims.session_id
                   AND s.lease_owner = file_claims.lease_owner
                   AND s.lease_expires_at > ?
             )
            """,
            (_iso(now),),
        )

    async def assert_owned(
        self,
        session_id: str,
        lease_owner: str,
        path: str,
        now: datetime,
    ) -> None:
        await self.db.run(lambda conn: self._assert_owned(conn, session_id, lease_owner, path, now))

    def _assert_owned(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        lease_owner: str,
        path: str,
        now: datetime,
    ) -> None:
        row = conn.execute(
            "SELECT * FROM file_claims WHERE session_id = ? AND path = ?",
            (session_id, path),
        ).fetchone()
        if row is None:
            raise ClaimError(f"no claim held for {path!r}")
        claim = ClaimRecord.from_row(row)
        if claim.lease_owner != lease_owner or claim.expires_at <= now:
            raise ClaimError(f"claim for {path!r} is stale or owned by another run")
        lease_row = conn.execute(
            "SELECT lease_owner, lease_expires_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        lease_expires = _parse_dt(lease_row["lease_expires_at"]) if lease_row else None
        if (
            lease_row is None
            or lease_row["lease_owner"] != lease_owner
            or lease_expires is None
            or lease_expires <= now
        ):
            raise ClaimError("session lease was taken over or expired; write is fenced off")

    async def release_owner(self, session_id: str, lease_owner: str) -> int:
        return await self.db.run(lambda conn: self._release_owner(conn, session_id, lease_owner))

    def _release_owner(self, conn: sqlite3.Connection, session_id: str, lease_owner: str) -> int:
        cursor = conn.execute(
            "DELETE FROM file_claims WHERE session_id = ? AND lease_owner = ?",
            (session_id, lease_owner),
        )
        return cursor.rowcount

    async def release_one(self, session_id: str, lease_owner: str, path: str) -> None:
        await self.db.run(lambda conn: self._release_one(conn, session_id, lease_owner, path))

    def _release_one(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        lease_owner: str,
        path: str,
    ) -> None:
        conn.execute(
            "DELETE FROM file_claims WHERE session_id = ? AND lease_owner = ? AND path = ?",
            (session_id, lease_owner, path),
        )

    async def renew_owner(
        self, session_id: str, lease_owner: str, ttl_seconds: float, now: datetime
    ) -> int:
        return await self.db.run(
            lambda conn: self._renew_owner(conn, session_id, lease_owner, ttl_seconds, now)
        )

    def _renew_owner(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        lease_owner: str,
        ttl_seconds: float,
        now: datetime,
    ) -> int:
        cursor = conn.execute(
            """
            UPDATE file_claims SET expires_at = ?
             WHERE session_id = ? AND lease_owner = ?
            """,
            (_iso(now + timedelta(seconds=ttl_seconds)), session_id, lease_owner),
        )
        return cursor.rowcount

    async def list_for_owner(self, session_id: str, lease_owner: str) -> list[ClaimRecord]:
        return await self.db.run(lambda conn: self._list_for_owner(conn, session_id, lease_owner))

    def _list_for_owner(
        self, conn: sqlite3.Connection, session_id: str, lease_owner: str
    ) -> list[ClaimRecord]:
        rows = conn.execute(
            "SELECT * FROM file_claims WHERE session_id = ? AND lease_owner = ?",
            (session_id, lease_owner),
        ).fetchall()
        return [ClaimRecord.from_row(row) for row in rows]

    async def list_all(self) -> list[ClaimRecord]:
        return await self.db.run(self._list_all)

    def _list_all(self, conn: sqlite3.Connection) -> list[ClaimRecord]:
        rows = conn.execute("SELECT * FROM file_claims ORDER BY acquired_at").fetchall()
        return [ClaimRecord.from_row(row) for row in rows]


class SecretNameRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, name: str, now: datetime) -> None:
        await self.db.run(lambda conn: self._add(conn, name, now))

    def _add(self, conn: sqlite3.Connection, name: str, now: datetime) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO secret_names (name, created_at) VALUES (?, ?)",
            (name, _iso(now)),
        )

    async def delete(self, name: str) -> None:
        await self.db.run(lambda conn: self._delete(conn, name))

    def _delete(self, conn: sqlite3.Connection, name: str) -> None:
        conn.execute("DELETE FROM secret_names WHERE name = ?", (name,))

    async def list_names(self) -> list[str]:
        return await self.db.run(self._list_names)

    def _list_names(self, conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute("SELECT name FROM secret_names ORDER BY name").fetchall()
        return [str(row["name"]) for row in rows]
