"""Atomic JSON writes for exports and reports."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json(path: str | Path, data: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
