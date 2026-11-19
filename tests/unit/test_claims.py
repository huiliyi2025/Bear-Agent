from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bear_agent.core.clock import FakeClock
from bear_agent.core.errors import ClaimConflict, ClaimError
from bear_agent.policy.path_guard import PathAccessError, PathGuard
from bear_agent.sessions.claims import ClaimService
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database
from bear_agent.storage.security_repositories import ClaimRepository


async def _setup(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    clock = FakeClock(datetime.now(UTC))
    sessions = SessionService(db, clock)
    session_a = await sessions.create(cwd=tmp_path, model="m", provider="p")
    session_b = await sessions.create(cwd=tmp_path, model="m", provider="p")
    owner_a = "owner-a"
    owner_b = "owner-b"
    epoch_a = await sessions.acquire_lease(session_a.id, owner_a, 300)
    epoch_b = await sessions.acquire_lease(session_b.id, owner_b, 300)
    claims = ClaimService(ClaimRepository(db), PathGuard(tmp_path), clock=clock, ttl_seconds=60)
    return db, sessions, claims, session_a, session_b, owner_a, owner_b, epoch_a, epoch_b


@pytest.mark.asyncio
async def test_file_claim_conflict_and_release(tmp_path: Path) -> None:
    db, _sessions, claims, a, b, owner_a, owner_b, _, _ = await _setup(tmp_path)
    try:
        await claims.acquire(a.id, owner_a, "file.txt")
        with pytest.raises(ClaimConflict):
            await claims.acquire(b.id, owner_b, "file.txt")
        await claims.release_owner(a.id, owner_a)
        await claims.acquire(b.id, owner_b, "file.txt")
        assert len(await claims.list_for_owner(b.id, owner_b)) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_tree_claim_conflicts_with_file(tmp_path: Path) -> None:
    db, _sessions, claims, a, b, owner_a, owner_b, _, _ = await _setup(tmp_path)
    try:
        await claims.acquire(a.id, owner_a, ".", scope="tree")
        with pytest.raises(ClaimConflict):
            await claims.acquire(b.id, owner_b, "src/app.py")
        await claims.release_owner(a.id, owner_a)
        await claims.acquire(b.id, owner_b, "src/app.py")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_claim_expiry_allows_reclaim(tmp_path: Path) -> None:
    db, _sessions, claims, a, b, owner_a, owner_b, _, _ = await _setup(tmp_path)
    try:
        await claims.acquire(a.id, owner_a, "file.txt")
        claims.clock.advance(120)  # type: ignore[attr-defined]
        # The second acquisition purges the expired claim.
        await claims.acquire(b.id, owner_b, "file.txt")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_old_lease_owner_is_fenced(tmp_path: Path) -> None:
    db, sessions, claims, a, _b, owner_a, owner_b, epoch_a, _ = await _setup(tmp_path)
    try:
        await claims.acquire(a.id, owner_a, "file.txt")
        await sessions.release_lease(a.id, owner_a, epoch_a)
        await sessions.acquire_lease(a.id, owner_b, 300)
        with pytest.raises(ClaimError, match=r"fenced|taken over|stale"):
            await claims.assert_owned(a.id, owner_a, "file.txt")
        with pytest.raises(ClaimError):
            await claims.acquire(a.id, owner_a, "other.txt")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_claim_path_must_stay_inside_workspace(tmp_path: Path) -> None:
    db, _sessions, claims, a, _b, owner_a, _owner_b, _, _ = await _setup(tmp_path)
    try:
        with pytest.raises(PathAccessError):
            await claims.acquire(a.id, owner_a, "../outside.txt")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_claim_conflict_across_connections(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db2 = Database(tmp_path / "db.sqlite")
    clock = FakeClock(datetime.now(UTC))
    try:
        sessions = SessionService(db, clock)
        session_a = await sessions.create(cwd=tmp_path, model="m", provider="p")
        session_b = await sessions.create(cwd=tmp_path, model="m", provider="p")
        owner_a, owner_b = "owner-a", "owner-b"
        await sessions.acquire_lease(session_a.id, owner_a, 300)
        await sessions.acquire_lease(session_b.id, owner_b, 300)
        claims_a = ClaimService(
            ClaimRepository(db), PathGuard(tmp_path), clock=clock, ttl_seconds=60
        )
        claims_b = ClaimService(
            ClaimRepository(db2), PathGuard(tmp_path), clock=clock, ttl_seconds=60
        )
        await claims_a.acquire(session_a.id, owner_a, "same.txt")
        with pytest.raises(ClaimConflict):
            await claims_b.acquire(session_b.id, owner_b, "same.txt")
    finally:
        db.close()
        db2.close()


@pytest.mark.asyncio
async def test_expired_session_lease_is_fenced(tmp_path: Path) -> None:
    db, _sessions, claims, a, _b, owner_a, _owner_b, _, _ = await _setup(tmp_path)
    try:
        await claims.acquire(a.id, owner_a, "file.txt")
        past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()

        await db.run(
            lambda conn: conn.execute(
                "UPDATE sessions SET lease_expires_at = ? WHERE id = ?",
                (past, a.id),
            )
        )
        with pytest.raises(ClaimError):
            await claims.assert_owned(a.id, owner_a, "file.txt")
        with pytest.raises(ClaimError):
            await claims.acquire(a.id, owner_a, "new.txt")
    finally:
        db.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows case-insensitive filesystem")
@pytest.mark.asyncio
async def test_claim_paths_are_case_insensitive_on_windows(tmp_path: Path) -> None:
    db, _sessions, claims, a, b, owner_a, owner_b, _, _ = await _setup(tmp_path)
    try:
        await claims.acquire(a.id, owner_a, "File.TXT")
        with pytest.raises(ClaimConflict):
            await claims.acquire(b.id, owner_b, "file.txt")
    finally:
        db.close()
