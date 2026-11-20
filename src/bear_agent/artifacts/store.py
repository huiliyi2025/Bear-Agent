"""Content-addressed artifact store.

Compaction moves old tool output out of the prompt but must not destroy it.
Artifacts are written to a content-addressed file (``<root>/<aa>/<sha256>.txt``)
with an atomic temp-file + fsync + replace sequence, then registered in SQLite.
Reads verify the hash, so a truncated or edited file is reported instead of
being silently injected back into a model prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from bear_agent.core.errors import ArtifactCorrupt, ArtifactError
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.ids import new_id
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.storage.continuity_repositories import (
    ArtifactRecord,
    ArtifactRepository,
)

TEXT_SUFFIX = ".txt"


class ArtifactStore:
    def __init__(
        self,
        root: str | Path,
        repository: ArtifactRepository,
        *,
        redactor: SecretRedactor | None = None,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.root = Path(root).resolve()
        self.repository = repository
        self.redactor = redactor or SecretRedactor()
        self.max_bytes = max_bytes

    async def put_text(
        self,
        session_id: str,
        text: str,
        *,
        kind: str,
        mime: str = "text/plain; charset=utf-8",
        metadata: dict[str, Any] | None = None,
        fence: LeaseFence | None = None,
    ) -> ArtifactRecord:
        safe_text = self.redactor.redact(text)
        data = safe_text.encode("utf-8", errors="replace")
        original_bytes = len(data)
        truncated = False
        if len(data) > self.max_bytes:
            data = data[: self.max_bytes]
            truncated = True
        digest = hashlib.sha256(data).hexdigest()
        relative = f"{digest[:2]}/{digest}{TEXT_SUFFIX}"
        target = self.root / relative
        await asyncio.to_thread(self._write_atomic, target, data)
        record = ArtifactRecord(
            id=new_id("art"),
            session_id=session_id,
            sha256=digest,
            path=relative,
            size=len(data),
            mime=mime,
            kind=kind,
            metadata={
                **(metadata or {}),
                "original_bytes": original_bytes,
                "truncated": truncated,
            },
        )
        try:
            return await self.repository.create(record, fence=fence)
        except BaseException:
            # Do not leave a file that no row references. A concurrent writer
            # may share the same content address; deleting is still safe only
            # if this call created it, which we cannot know reliably. Keep the
            # file in that rare case rather than risk deleting live data.
            raise

    async def put_bytes(
        self,
        session_id: str,
        data: bytes,
        *,
        kind: str,
        mime: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
        fence: LeaseFence | None = None,
    ) -> ArtifactRecord:
        return await self.put_text(
            session_id,
            data.decode("utf-8", errors="replace"),
            kind=kind,
            mime=mime,
            metadata=metadata,
            fence=fence,
        )

    async def get_text(self, artifact_id: str) -> str:
        record = await self.repository.get(artifact_id)
        if record is None:
            raise ArtifactError(f"unknown artifact: {artifact_id}")
        return await asyncio.to_thread(self._read_verified, record)

    async def read_record_text(self, record: ArtifactRecord) -> str:
        return await asyncio.to_thread(self._read_verified, record)

    def resolve(self, record: ArtifactRecord) -> Path:
        candidate = (self.root / record.path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactError(f"artifact path escapes the store: {record.path}") from exc
        return candidate

    def _write_atomic(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=".tmp-",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            self._fsync_dir(target.parent)
        except BaseException:
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    temp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        # Directory fsync is not supported on every platform (notably Windows).
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _read_verified(self, record: ArtifactRecord) -> str:
        path = self.resolve(record)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ArtifactError(f"artifact file is unavailable: {record.id}") from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != record.sha256:
            raise ArtifactCorrupt(
                f"artifact {record.id} failed integrity check "
                f"(expected {record.sha256[:12]}, got {digest[:12]})"
            )
        return data.decode("utf-8", errors="replace")
