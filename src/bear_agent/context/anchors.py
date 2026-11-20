"""Durable context anchors.

Anchors are the small, high-salience facts that must survive context
compaction: the user's goal, hard constraints and explicit notes. They live in
SQLite so a process restart does not lose them, are deduplicated by normalized
text, and render into a token-bounded ``<context-anchors>`` block that the
compactor pins in front of the history.

The ledger deliberately contains no audit logic: event recording is optional
and injected, callers own security auditing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from bear_agent.context.tokens import estimate_text_tokens
from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.ids import new_id
from bear_agent.core.messages import Message
from bear_agent.observability.recorder import EventRecorder
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.storage.continuity_repositories import AnchorRecord, AnchorRepository

MAX_ANCHOR_CHARS = 1000
MAX_ACTIVE_ANCHORS = 200
SALIENCE_BUMP = 0.1
MAX_SALIENCE = 2.0

MIN_RENDER_TOKENS = 12
MIN_RENDER_CHARS = 40

_ASCII_CONSTRAINT = re.compile(
    r"\b(?:must|never|always|requir\w*|constraint\w*)\b",
    re.IGNORECASE,
)
_CJK_CONSTRAINT_MARKERS = (
    "不要",
    "必须",
    "禁止",
    "约束",
    "要求",
    "只能",
    "不能改",
)


def contains_constraint_marker(text: str) -> bool:
    """True when ``text`` contains a hard requirement marker.

    Used both for anchor extraction and for deterministic L2 summaries, so the
    two layers agree on what counts as a constraint.
    """
    if not text:
        return False
    if _ASCII_CONSTRAINT.search(text):
        return True
    return any(marker in text for marker in _CJK_CONSTRAINT_MARKERS)


def truncate_text_to_tokens(text: str, max_tokens: int) -> str:
    """Return the longest prefix of ``text`` that fits ``max_tokens``."""
    if max_tokens <= 0 or not text:
        return ""
    if estimate_text_tokens(text) <= max_tokens:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _clip(text: str) -> str:
    return text.strip()[:MAX_ANCHOR_CHARS]


class AnchorLedger:
    def __init__(
        self,
        repository: AnchorRepository,
        *,
        clock: Clock | None = None,
        recorder: EventRecorder | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.repository = repository
        self.clock = clock or SystemClock()
        self.recorder = recorder
        self.redactor = redactor or SecretRedactor()

    async def add(
        self,
        session_id: str,
        text: str,
        *,
        kind: str = "note",
        source_message_seq: int | None = None,
        salience: float = 1.0,
        fence: LeaseFence | None = None,
    ) -> AnchorRecord:
        record, created = await self._add(
            session_id,
            text,
            kind=kind,
            source_message_seq=source_message_seq,
            salience=salience,
            fence=fence,
        )
        if created:
            await self._record_added(session_id, [record], fence=fence)
        return record

    async def remove(
        self, session_id: str, anchor_id: str, *, fence: LeaseFence | None = None
    ) -> bool:
        return await self.repository.deactivate(session_id, anchor_id, fence=fence)

    async def active(self, session_id: str) -> list[AnchorRecord]:
        return await self.repository.list_active(session_id, limit=MAX_ACTIVE_ANCHORS)

    async def observe_messages(
        self,
        session_id: str,
        messages: Sequence[Message],
        *,
        fence: LeaseFence | None = None,
    ) -> int:
        """Create anchors inferred from the transcript, idempotently.

        The first user message becomes the goal; later user messages that carry
        a hard requirement marker become constraints. Identical anchors are
        deduplicated by the ledger, so calling this once per prepare is safe.
        """
        users = [message for message in messages if message.role == "user"]
        if not users:
            return 0

        created: list[AnchorRecord] = []
        goal_text = _clip(users[0].text_content())
        if goal_text:
            record, was_created = await self._add(
                session_id,
                goal_text,
                kind="goal",
                source_message_seq=users[0].seq,
                salience=1.0,
                fence=fence,
            )
            if was_created:
                created.append(record)

        for message in users[1:]:
            text = _clip(message.text_content())
            if not text or not contains_constraint_marker(text):
                continue
            record, was_created = await self._add(
                session_id,
                text,
                kind="constraint",
                source_message_seq=message.seq,
                salience=0.9,
                fence=fence,
            )
            if was_created:
                created.append(record)

        if created:
            await self._record_added(session_id, created, fence=fence)
        return len(created)

    def render(self, anchors: Sequence[AnchorRecord], max_tokens: int) -> str:
        """Render anchors as a bounded, salience-ordered bullet block.

        Highest-salience anchors are kept first. When the budget is tight the
        lowest-salience anchors are dropped, and an anchor that is the only one
        that can fit is truncated to the remaining budget. The returned string
        never exceeds ``max_tokens`` by the estimator.
        """
        if max_tokens <= 0 or not anchors:
            return ""
        ordered = sorted(anchors, key=lambda anchor: (-anchor.salience, anchor.created_at))
        opening = "<context-anchors>"
        closing = "</context-anchors>"
        wrapper_tokens = estimate_text_tokens(f"{opening}\n{closing}")
        remaining = max_tokens - wrapper_tokens
        if remaining <= 0:
            return ""

        bullets: list[str] = []
        for anchor in ordered:
            bullet = f"- [{anchor.kind}] {anchor.text}"
            bullet_tokens = estimate_text_tokens(bullet) + 1  # trailing newline
            if bullet_tokens <= remaining:
                bullets.append(bullet)
                remaining -= bullet_tokens
                continue
            if remaining < MIN_RENDER_TOKENS:
                break
            prefix = truncate_text_to_tokens(anchor.text, remaining - 8)
            if len(prefix) >= MIN_RENDER_CHARS:
                bullets.append(f"- [{anchor.kind}] {prefix}...")
            break

        while bullets:
            block = f"{opening}\n" + "\n".join(bullets) + f"\n{closing}"
            if estimate_text_tokens(block) <= max_tokens:
                return block
            bullets.pop()
        return ""

    # ------------------------------------------------------------------ helpers

    async def _add(
        self,
        session_id: str,
        text: str,
        *,
        kind: str,
        source_message_seq: int | None,
        salience: float,
        fence: LeaseFence | None,
    ) -> tuple[AnchorRecord, bool]:
        cleaned = _clip(self.redactor.redact(text))
        if not cleaned:
            raise ValueError("anchor text must not be empty")
        normalized = _normalize(cleaned)
        now = self.clock.now()
        for existing in await self.active(session_id):
            if _normalize(existing.text) != normalized:
                continue
            updated = AnchorRecord(
                id=existing.id,
                session_id=existing.session_id,
                kind=existing.kind,
                text=existing.text,
                source_message_seq=(
                    existing.source_message_seq
                    if existing.source_message_seq is not None
                    else source_message_seq
                ),
                salience=min(MAX_SALIENCE, max(existing.salience, salience) + SALIENCE_BUMP),
                active=True,
                created_at=existing.created_at,
                updated_at=now,
            )
            return await self.repository.upsert(updated, fence=fence), False

        record = AnchorRecord(
            id=new_id("anc"),
            session_id=session_id,
            kind=kind,
            text=cleaned,
            source_message_seq=source_message_seq,
            salience=salience,
            active=True,
            created_at=now,
            updated_at=now,
        )
        return await self.repository.upsert(record, fence=fence), True

    async def _record_added(
        self,
        session_id: str,
        records: Sequence[AnchorRecord],
        *,
        fence: LeaseFence | None,
    ) -> None:
        if self.recorder is None or not records:
            return
        await self.recorder.record(
            session_id,
            EventType.CONTEXT_ANCHOR_ADDED,
            payload={
                "ids": [record.id for record in records],
                "count": len(records),
                "kinds": sorted({record.kind for record in records}),
            },
            fence=fence,
        )
