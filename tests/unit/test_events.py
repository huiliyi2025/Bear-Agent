from __future__ import annotations

import pytest

from bear_agent.core.events import PERSISTED_EVENT_TYPES, Event, EventType
from bear_agent.observability.bus import EventBus


def test_persisted_set() -> None:
    assert EventType.MESSAGE_USER in PERSISTED_EVENT_TYPES
    assert EventType.MODEL_TEXT_DELTA not in PERSISTED_EVENT_TYPES


@pytest.mark.asyncio
async def test_bus_isolates_failing_subscriber() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def broken(event: Event) -> None:
        raise RuntimeError("observer failure")

    async def good(event: Event) -> None:
        received.append(event)

    bus.subscribe(broken)
    bus.subscribe(good)
    event = Event(session_id="s", type=EventType.RUN_STARTED)
    await bus.publish(event)
    assert received == [event]


@pytest.mark.asyncio
async def test_unsubscribe() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    unsubscribe = bus.subscribe(handler)
    unsubscribe()
    await bus.publish(Event(session_id="s", type=EventType.RUN_STARTED))
    assert received == []
