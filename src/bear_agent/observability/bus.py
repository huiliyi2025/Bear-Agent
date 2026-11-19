"""In-process event fan-out for UI and telemetry.

Subscribers are isolated: one failing observer never breaks a run. Runtime
correctness never depends on a subscriber.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from bear_agent.core.events import Event

logger = logging.getLogger(__name__)

EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        self._handlers.append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        if not self._handlers:
            return
        results = await asyncio.gather(
            *(handler(event) for handler in list(self._handlers)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("event handler failed: %s", result)
