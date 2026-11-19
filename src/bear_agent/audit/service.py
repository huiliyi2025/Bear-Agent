"""Append-only, hash-chained security audit log.

Each record includes the previous record's hash. Verification recomputes the
chain; editing or deleting a record breaks it. This is *local* integrity, not
tamper-proof storage: an attacker with database write access can recompute the
entire chain. An external anchor/signature is intentionally out of scope.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.errors import AuditError
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.storage.database import Database, transaction

GENESIS_HASH = "0" * 64


def _canonical_detail(detail: dict[str, Any]) -> str:
    return json.dumps(
        detail, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _chain_hash(
    *,
    prev_hash: str,
    actor: str,
    action: str,
    resource: str | None,
    session_id: str | None,
    canonical_detail: str,
    created_at: str,
) -> str:
    payload = json.dumps(
        {
            "prev_hash": prev_hash,
            "actor": actor,
            "action": action,
            "resource": resource or "",
            "session_id": session_id or "",
            "detail": canonical_detail,
            "created_at": created_at,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    session_id: str | None
    actor: str
    action: str
    resource: str | None
    detail: dict[str, Any]
    prev_hash: str
    hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AuditVerification:
    ok: bool
    records: int
    first_bad_id: int | None = None
    reason: str = ""


class AuditService:
    def __init__(
        self,
        db: Database,
        *,
        redactor: SecretRedactor | None = None,
        clock: Clock | None = None,
        enabled: bool = True,
    ) -> None:
        self.db = db
        self.redactor = redactor or SecretRedactor()
        self.clock = clock or SystemClock()
        self.enabled = enabled

    async def append(
        self,
        *,
        actor: str,
        action: str,
        resource: str | None = None,
        detail: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> AuditEntry | None:
        if not self.enabled:
            return None
        safe_detail = self.redactor.redact_obj(detail or {})
        try:
            return await self.db.run(
                lambda conn: self._append(conn, actor, action, resource, safe_detail, session_id)
            )
        except AuditError:
            raise
        except Exception as exc:  # audit failure is security-relevant
            raise AuditError(f"failed to append audit record: {exc}") from exc

    def _append(
        self,
        conn: sqlite3.Connection,
        actor: str,
        action: str,
        resource: str | None,
        detail: dict[str, Any],
        session_id: str | None,
    ) -> AuditEntry:
        created_at = self.clock.now()
        safe_resource = self.redactor.redact(resource) if resource else None
        canonical = _canonical_detail(detail)
        with transaction(conn):
            row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = str(row["hash"]) if row else GENESIS_HASH
            digest = _chain_hash(
                prev_hash=prev_hash,
                actor=actor,
                action=action,
                resource=safe_resource,
                session_id=session_id,
                canonical_detail=canonical,
                created_at=created_at.isoformat(),
            )
            cursor = conn.execute(
                """
                INSERT INTO audit_log (
                    session_id, actor, action, resource, detail_json,
                    prev_hash, hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    actor,
                    action,
                    safe_resource,
                    canonical,
                    prev_hash,
                    digest,
                    created_at.isoformat(),
                ),
            )
            return AuditEntry(
                id=int(cursor.lastrowid or 0),
                session_id=session_id,
                actor=actor,
                action=action,
                resource=safe_resource,
                detail=detail,
                prev_hash=prev_hash,
                hash=digest,
                created_at=created_at,
            )

    async def verify(self) -> AuditVerification:
        return await self.db.run(self._verify)

    def _verify(self, conn: sqlite3.Connection) -> AuditVerification:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id ASC").fetchall()
        prev_hash = GENESIS_HASH
        for row in rows:
            try:
                detail = json.loads(row["detail_json"])
            except (TypeError, ValueError):
                return AuditVerification(
                    False,
                    len(rows),
                    int(row["id"]),
                    "record detail is not valid JSON",
                )
            canonical = _canonical_detail(detail)
            expected = _chain_hash(
                prev_hash=prev_hash,
                actor=row["actor"],
                action=row["action"],
                resource=row["resource"],
                session_id=row["session_id"],
                canonical_detail=canonical,
                created_at=row["created_at"],
            )
            if row["prev_hash"] != prev_hash:
                return AuditVerification(
                    False,
                    len(rows),
                    int(row["id"]),
                    "previous hash does not match the preceding record",
                )
            if row["hash"] != expected:
                return AuditVerification(
                    False,
                    len(rows),
                    int(row["id"]),
                    "record hash does not match its content",
                )
            prev_hash = str(row["hash"])
        return AuditVerification(True, len(rows))

    async def tail(self, limit: int = 50) -> list[AuditEntry]:
        return await self.db.run(lambda conn: self._tail(conn, limit))

    def _tail(self, conn: sqlite3.Connection, limit: int) -> list[AuditEntry]:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        entries: list[AuditEntry] = []
        for row in rows:
            try:
                detail = json.loads(row["detail_json"])
            except (TypeError, ValueError):
                detail = {"_malformed": True}
            try:
                created_at = datetime.fromisoformat(row["created_at"])
            except (TypeError, ValueError):
                created_at = datetime.min
            entries.append(
                AuditEntry(
                    id=int(row["id"]),
                    session_id=row["session_id"],
                    actor=row["actor"],
                    action=row["action"],
                    resource=row["resource"],
                    detail=detail,
                    prev_hash=row["prev_hash"],
                    hash=row["hash"],
                    created_at=created_at,
                )
            )
        return entries

    async def require(self, record: AuditEntry | None) -> None:
        if self.enabled and record is None:
            raise AuditError("security audit record was not written")
