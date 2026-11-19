"""Event recording boundary.

Fact events are appended to the session log and then published. Delta and UI
events are published only. This keeps persistence volume bounded while still
giving surfaces a live stream.
"""

from __future__ import annotations

from typing import Any

from bear_agent.core.errors import StorageError
from bear_agent.core.events import PERSISTED_EVENT_TYPES, Event, EventType
from bear_agent.observability.bus import EventBus
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.service import SessionService


class EventRecorder:
    def __init__(
        self,
        sessions: SessionService,
        bus: EventBus,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.sessions = sessions
        self.bus = bus
        self.redactor = redactor or SecretRedactor()

    async def record(
        self,
        session_id: str,
        event_type: EventType,
        *,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        persist: bool | None = None,
    ) -> Event:
        should_persist = event_type in PERSISTED_EVENT_TYPES if persist is None else persist
        safe_payload = self.redactor.redact_obj(payload or {})
        if should_persist:
            event = await self.sessions.append_event(
                session_id,
                event_type,
                payload=safe_payload,
                run_id=run_id,
                turn_id=turn_id,
            )
            if event is None:
                raise StorageError(f"event type {event_type.value!r} is not persistable")
        else:
            event = Event(
                session_id=session_id,
                type=event_type,
                payload=safe_payload,
                run_id=run_id,
                turn_id=turn_id,
            )
        await self.bus.publish(event)
        return event

    async def publish_message_event(self, event: Event) -> None:
        """Publish an event already persisted atomically with a message row."""
        await self.bus.publish(event)
