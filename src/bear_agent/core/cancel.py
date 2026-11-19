"""Cooperative cancellation.

A ``CancelToken`` is passed to providers, tools and the runtime. It can be
observed (``is_cancelled``), awaited (``wait``) or asserted (``raise_if_cancelled``).
Runtime cancellation is implemented by cancelling the run task as well, which
also aborts in-flight HTTP streams.
"""

from __future__ import annotations

import asyncio


class RunCancelled(Exception):
    """Raised when work is cancelled by the user or by a supervisor."""


class CancelToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise RunCancelled("run cancelled")

    async def wait(self) -> None:
        await self._event.wait()
