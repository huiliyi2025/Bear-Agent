"""Identifier helpers.

IDs are intentionally opaque and prefixed so logs, events and database rows are
easy to distinguish. UUIDv4 is used because the canonical ordering used by the
harness is the per-session event sequence, not the ID itself.
"""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    """Return a prefixed unique identifier, e.g. ``ses_1a2b...``."""
    if not prefix:
        raise ValueError("prefix must not be empty")
    return f"{prefix}_{uuid.uuid4().hex}"
