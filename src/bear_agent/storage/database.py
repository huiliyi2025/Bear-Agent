"""SQLite connection management.

SQLite in WAL mode is the durable source of truth for sessions, events, tasks
(later) and usage. Writes are serialized per process and short; reads use the
same connection because the harness is single-process in phase 1. The async
facade runs blocking SQLite work in a worker thread so the event loop stays
responsive.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from bear_agent.storage.migrations import migrate

T = TypeVar("T")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Explicit transaction on a connection opened with ``isolation_level=None``."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._thread_lock = threading.RLock()
        self._async_lock: asyncio.Lock | None = None
        try:
            migrate(self._conn)
        except BaseException:
            # A rejected/incompatible database must not leave an open handle
            # (and its WAL files) behind.
            self._conn.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run a blocking function with exclusive access to the connection."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        async with self._async_lock:
            return await asyncio.to_thread(self._run_sync, fn)

    def _run_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._thread_lock:
            return fn(self._conn)

    def close(self) -> None:
        with self._thread_lock:
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
