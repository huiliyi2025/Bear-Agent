from __future__ import annotations

import json
from pathlib import Path

import pytest

from bear_agent.audit.service import AuditService
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.storage.database import Database


@pytest.mark.asyncio
async def test_audit_chain_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        audit = AuditService(db)
        first = await audit.append(
            actor="agent", action="policy.decision", resource="bash", detail={"d": 1}
        )
        second = await audit.append(
            actor="agent", action="tool.finished", resource="bash", detail={"d": 2}
        )
        assert first is not None and second is not None
        assert second.prev_hash == first.hash
        verification = await audit.verify()
        assert verification.ok
        assert verification.records == 2
        tail = await audit.tail(1)
        assert tail[0].action == "tool.finished"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_audit_detects_tampered_detail(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        audit = AuditService(db)
        await audit.append(actor="agent", action="a", detail={"value": 1})
        await audit.append(actor="agent", action="b", detail={"value": 2})

        await db.run(
            lambda conn: conn.execute(
                "UPDATE audit_log SET detail_json = ? WHERE id = 1",
                (json.dumps({"value": 999}),),
            )
        )
        verification = await audit.verify()
        assert not verification.ok
        assert verification.first_bad_id == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_audit_detects_tampered_hash(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        audit = AuditService(db)
        await audit.append(actor="agent", action="a", detail={})

        await db.run(lambda conn: conn.execute("UPDATE audit_log SET hash = 'bad' WHERE id = 1"))
        verification = await audit.verify()
        assert not verification.ok
        assert verification.first_bad_id == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_audit_redacts_details(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        redactor = SecretRedactor()
        redactor.register("x", "super-secret")
        audit = AuditService(db, redactor=redactor)
        await audit.append(actor="agent", action="a", detail={"api_key": "super-secret"})
        entry = (await audit.tail(1))[0]
        assert "super-secret" not in json.dumps(entry.detail)
        assert (await audit.verify()).ok
    finally:
        db.close()


@pytest.mark.asyncio
async def test_audit_disabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        audit = AuditService(db, enabled=False)
        assert await audit.append(actor="agent", action="a", detail={}) is None
        assert (await audit.verify()).ok
    finally:
        db.close()


@pytest.mark.asyncio
async def test_audit_detects_tampered_session_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        audit = AuditService(db)
        await audit.append(actor="agent", action="a", detail={}, session_id="session-a")
        await db.run(
            lambda conn: conn.execute("UPDATE audit_log SET session_id = 'session-b' WHERE id = 1")
        )
        verification = await audit.verify()
        assert not verification.ok
        assert verification.first_bad_id == 1
    finally:
        db.close()
