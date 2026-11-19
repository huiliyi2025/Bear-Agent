"""Session record.

A session owns the append-only transcript and is the unit of resumption,
persistence and concurrency control.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from bear_agent.core.clock import Clock
from bear_agent.core.ids import new_id

SessionStatus = Literal["active", "idle", "archived"]


def project_id_for(cwd: str | Path) -> str:
    resolved = Path(cwd).resolve()
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:32]


class SessionRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ses"))
    project_id: str
    cwd: str
    title: str | None = None
    status: SessionStatus = "active"
    model: str | None = None
    provider: str | None = None
    last_seq: int = 0
    lease_owner: str | None = None
    lease_epoch: int = 0
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def new(
        cls,
        *,
        cwd: str | Path,
        model: str | None,
        provider: str | None,
        clock: Clock | None = None,
    ) -> SessionRecord:
        now = clock.now() if clock else datetime.now(UTC)
        resolved = str(Path(cwd).resolve())
        return cls(
            project_id=project_id_for(resolved),
            cwd=resolved,
            model=model,
            provider=provider,
            created_at=now,
            updated_at=now,
        )
