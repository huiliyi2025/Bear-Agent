"""Cross-session file claims.

A claim is a lease-backed reservation. The storage layer fences writes by
checking that the claim's `lease_owner` still matches the session's current
lease owner, so a process whose run lease was taken over cannot keep writing.

File tools claim their target; the shell tool claims the workspace tree, which
serializes shell writes against all file claims.
"""

from __future__ import annotations

import os
from datetime import timedelta

from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.ids import new_id
from bear_agent.policy.path_guard import PathGuard
from bear_agent.storage.security_repositories import ClaimRecord, ClaimRepository


class ClaimService:
    def __init__(
        self,
        repository: ClaimRepository,
        path_guard: PathGuard,
        *,
        clock: Clock | None = None,
        ttl_seconds: float = 600.0,
    ) -> None:
        self.repository = repository
        self.path_guard = path_guard
        self.clock = clock or SystemClock()
        self.ttl_seconds = ttl_seconds

    def normalize(self, raw_path: str) -> str:
        resolved = self.path_guard.resolve(raw_path, mode="write")
        relative = resolved.relative_to(self.path_guard.cwd)
        # Windows paths are case-insensitive; normalize so File.TXT and
        # file.txt cannot hold independent claims for the same file.
        normalized = os.path.normcase(relative.as_posix()).replace("\\", "/")
        return normalized or "."

    async def acquire(
        self,
        session_id: str,
        lease_owner: str,
        raw_path: str,
        *,
        scope: str = "file",
        mode: str = "exclusive",
    ) -> ClaimRecord:
        if scope not in {"file", "tree"}:
            raise ValueError(f"invalid claim scope: {scope}")
        if mode not in {"exclusive", "shared_read"}:
            raise ValueError(f"invalid claim mode: {mode}")
        now = self.clock.now()
        normalized = self.normalize(raw_path)
        record = ClaimRecord(
            id=new_id("claim"),
            session_id=session_id,
            lease_owner=lease_owner,
            path=normalized,
            scope=scope,
            mode=mode,
            acquired_at=now,
            expires_at=now + self._ttl(),
        )
        return await self.repository.acquire(record, now)

    async def assert_owned(self, session_id: str, lease_owner: str, raw_path: str) -> None:
        normalized = self.normalize(raw_path)
        await self.repository.assert_owned(session_id, lease_owner, normalized, self.clock.now())

    async def release_owner(self, session_id: str, lease_owner: str) -> int:
        return await self.repository.release_owner(session_id, lease_owner)

    async def release_stale(
        self,
        session_id: str,
        lease_owner: str,
        *,
        fence: LeaseFence | None = None,
    ) -> list[str]:
        """Drop claims left behind by an earlier lease owner.

        Called by recovery after the new owner has taken the lease. The
        session's own claims for the current owner are never touched. When a
        fence is supplied, the owner/epoch check and the delete are atomic.
        """
        return await self.repository.release_session_except_owner(
            session_id, lease_owner, fence=fence
        )

    async def release_one(self, session_id: str, lease_owner: str, raw_path: str) -> None:
        await self.repository.release_one(session_id, lease_owner, self.normalize(raw_path))

    async def renew_owner(
        self,
        session_id: str,
        lease_owner: str,
        ttl_seconds: float | None = None,
    ) -> int:
        return await self.repository.renew_owner(
            session_id,
            lease_owner,
            ttl_seconds if ttl_seconds is not None else self.ttl_seconds,
            self.clock.now(),
        )

    async def list_for_owner(self, session_id: str, lease_owner: str) -> list[ClaimRecord]:
        return await self.repository.list_for_owner(session_id, lease_owner)

    async def list_all(self) -> list[ClaimRecord]:
        return await self.repository.list_all()

    def _ttl(self) -> timedelta:
        return timedelta(seconds=self.ttl_seconds)
