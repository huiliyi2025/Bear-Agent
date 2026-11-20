"""Trace lifecycle, storage, concurrency, redaction and recovery diagnostics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bear_agent.config.models import ObservabilityConfig
from bear_agent.core.events import EventType
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.observability.trace import TraceService, span
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database
from bear_agent.storage.observability_repositories import TraceRepository


def _service(db: Database, **overrides: Any) -> TraceService:
    config = ObservabilityConfig(**overrides)
    return TraceService(db, config=config, redactor=SecretRedactor())


@pytest.mark.asyncio
async def test_trace_span_tree_and_event_linkage(tmp_path: Path) -> None:
    db = Database(tmp_path / "trace.db")
    try:
        sessions = SessionService(db)
        redactor = SecretRedactor()
        recorder = EventRecorder(sessions, EventBus(), redactor)
        tracer = _service(db)
        session = await sessions.create(cwd=tmp_path, model="m", provider="p")

        async with tracer.trace("run", kind="run", session_id=session.id, run_id="run-1") as scope:
            assert scope is not None
            async with span("tool.call", "tool", {"tool": "read_file"}):
                await asyncio.sleep(0.01)
                await recorder.record(
                    session.id,
                    EventType.TOOL_CALL_COMPLETED,
                    run_id="run-1",
                    payload={"name": "read_file"},
                )
            scope.set_attribute("turns", 1)
            scope.set_status("ok")
        assert scope is not None
        trace = await tracer.get(scope.trace_id)
        assert trace is not None
        assert trace["status"] == "ok"
        assert trace["run_id"] == "run-1"
        assert trace["span_count"] == 2
        assert trace["attributes"]["turns"] == 1

        spans = await tracer.spans(scope.trace_id)
        assert [span["name"] for span in spans] == ["run", "tool.call"]
        root = spans[0]
        child = spans[1]
        assert root["parent_span_id"] is None
        assert child["parent_span_id"] == root["span_id"]
        assert child["attributes"]["tool"] == "read_file"
        assert child["duration_ms"] >= 5

        hierarchy = await tracer.hierarchy(scope.trace_id)
        assert hierarchy is not None
        assert len(hierarchy["roots"]) == 1
        assert hierarchy["roots"][0]["children"][0]["name"] == "tool.call"
        assert hierarchy["orphan_spans"] == 0

        events = await tracer.events(scope.trace_id)
        assert any(event["type"] == EventType.TOOL_CALL_COMPLETED.value for event in events)
        exported = await tracer.export(scope.trace_id)
        assert exported is not None
        assert exported["trace"]["trace_id"] == scope.trace_id
        assert exported["redaction"]["prompts"] == "not-recorded"
        await tracer.close()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_error_and_cancel_status(tmp_path: Path) -> None:
    db = Database(tmp_path / "trace.db")
    try:
        tracer = _service(db)
        with pytest.raises(ValueError):
            async with tracer.trace("bad", session_id="s") as scope:
                assert scope is not None
                async with span("child", "tool"):
                    raise ValueError("boom token=sk-abcdefghijklmnop")
        rows = await tracer.list_traces()
        assert rows[0]["status"] == "error"
        assert rows[0]["error_type"] == "ValueError"
        assert "sk-abcdefghijklmnop" not in (rows[0]["error_message"] or "")
        assert "[REDACTED]" in (rows[0]["error_message"] or "")
        spans = await tracer.spans(rows[0]["trace_id"])
        assert {span["status"] for span in spans} == {"error"}

        with pytest.raises(asyncio.CancelledError):
            async with tracer.trace("cancelled", session_id="s"):
                raise asyncio.CancelledError()
        rows = await tracer.list_traces(status="cancelled")
        assert rows
        await tracer.close()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_trace_can_be_disabled_or_sampled_out(tmp_path: Path) -> None:
    db = Database(tmp_path / "trace.db")
    try:
        disabled = _service(db, traces_enabled=False)
        async with disabled.trace("noop", session_id="s") as scope:
            assert scope is None
            async with span("x", "internal"):
                pass
        assert await disabled.list_traces() == []

        sampled = _service(db, trace_sample_ratio=0.0)
        async with sampled.trace("noop", session_id="s") as scope:
            assert scope is None
        assert await sampled.list_traces() == []
        assert await sampled.stats() == {
            "traces": 0,
            "spans": 0,
            "running": 0,
            "incomplete": 0,
            "oldest": None,
            "newest": None,
        }
        await sampled.close()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_attributes_are_redacted_and_bounded(tmp_path: Path) -> None:
    db = Database(tmp_path / "trace.db")
    try:
        tracer = _service(db, trace_max_attributes_bytes=400)
        secret = "sk-" + "A" * 24
        async with tracer.trace(
            "run",
            session_id="s",
            attributes={
                "api_key": secret,
                "huge": "x" * 5_000,
                "count": 3,
            },
        ):
            pass
        rows = await tracer.list_traces()
        raw = str(rows[0]["attributes"])
        assert secret not in raw
        assert "[REDACTED]" in raw
        assert len(raw) < 2_000
        await tracer.close()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_concurrent_traces_keep_separate_parents(tmp_path: Path) -> None:
    db = Database(tmp_path / "trace.db")
    try:
        tracer = _service(db)

        async def one(index: int) -> str:
            async with tracer.trace(f"run-{index}", session_id=f"s{index}") as scope:
                assert scope is not None
                for child in range(3):
                    async with span(f"child-{child}", "internal"):
                        await asyncio.sleep(0)
                return scope.trace_id

        ids = await asyncio.gather(*(one(index) for index in range(4)))
        assert len(set(ids)) == 4
        for trace_id in ids:
            spans = await tracer.spans(trace_id)
            assert len(spans) == 4
            root_id = spans[0]["span_id"]
            assert all(span["parent_span_id"] == root_id for span in spans[1:])
        await tracer.close()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_incomplete_reconcile_prune_and_filters(tmp_path: Path) -> None:
    db = Database(tmp_path / "trace.db")
    try:
        repository = TraceRepository(db)
        now = datetime.now(UTC)
        await repository.insert_trace(
            trace_id="trc_old",
            session_id="s-old",
            run_id="run-old",
            task_id=None,
            name="old",
            kind="run",
            status="running",
            started_at=now - timedelta(days=3),
            attributes={},
            sampled=True,
            created_at=now - timedelta(days=3),
        )
        await repository.insert_trace(
            trace_id="trc_new",
            session_id="s-new",
            run_id="run-new",
            task_id="task-1",
            name="new",
            kind="run",
            status="ok",
            started_at=now,
            attributes={},
            sampled=True,
            created_at=now,
        )
        tracer = _service(db, incomplete_trace_grace_s=60, trace_retention_days=365)
        assert await tracer.reconcile_incomplete() == 1
        assert (await tracer.get("trc_old"))["status"] == "incomplete"  # type: ignore[index]
        assert len(await tracer.list_traces(session_id="s-new")) == 1
        assert len(await tracer.list_traces(run_id="run-old")) == 1
        assert len(await tracer.list_traces(task_id="task-1")) == 1
        assert len(await tracer.find_by_run("run-new")) == 1
        assert len(await tracer.find_by_session("s-new")) == 1
        assert await tracer.reconcile_incomplete() == 0

        old = _service(db, trace_retention_days=1)
        assert await old.prune() == 1
        assert await old.get("trc_old") is None
        assert (await old.stats())["traces"] == 1
        await old.close()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_orphan_span_is_reported(tmp_path: Path) -> None:
    db = Database(tmp_path / "trace.db")
    try:
        tracer = _service(db)
        async with tracer.trace("run", session_id="s") as scope:
            assert scope is not None
        assert scope is not None
        # Simulate a partially lost buffer.
        conn = db.connection
        conn.execute(
            """
            INSERT INTO spans (
                span_id, trace_id, parent_span_id, name, kind, status,
                started_at, ended_at, duration_ms, attributes_json,
                error_type, error_message, sequence, created_at
            ) VALUES ('spn_orphan', ?, 'spn_missing', 'lost', 'internal', 'ok',
                      ?, ?, 1, '{}', NULL, NULL, 99, ?)
            """,
            (
                scope.trace_id,
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        hierarchy = await tracer.hierarchy(scope.trace_id)
        assert hierarchy is not None
        assert hierarchy["orphan_spans"] == 1
        await tracer.close()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cancelled_finalization_is_retried_by_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "trace.db")
    try:
        tracer = _service(db)

        async def cancelled_finish(*args: object, **kwargs: object) -> None:
            raise asyncio.CancelledError()

        monkeypatch.setattr(tracer.repository, "finish_trace", cancelled_finish)
        with pytest.raises(asyncio.CancelledError):
            async with tracer.trace("run", session_id="s", run_id="r"):
                pass
        # The trace row must not stay 'running' after cancellation and close().
        monkeypatch.undo()
        await tracer.close()
        rows = await tracer.list_traces()
        assert rows
        assert rows[0]["status"] in {"incomplete", "ok"}
        assert rows[0]["ended_at"] is not None
        spans = await tracer.spans(rows[0]["trace_id"])
        assert spans and spans[0]["name"] == "run"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_trace_event_export_strips_task_content(tmp_path: Path) -> None:
    db = Database(tmp_path / "trace.db")
    try:
        sessions = SessionService(db)
        recorder = EventRecorder(sessions, EventBus())
        tracer = _service(db)
        session = await sessions.create(cwd=tmp_path, model="m", provider="p")
        async with tracer.trace("run", session_id=session.id) as scope:
            assert scope is not None
            await recorder.record(
                session.id,
                EventType.TASK_STARTED,
                payload={
                    "task_id": "t1",
                    "task_key": "T1",
                    "title": "secret task title",
                    "goal": "secret goal",
                    "reason": "secret reason",
                },
            )
        exported = await tracer.export(scope.trace_id)
        assert exported is not None
        payload = exported["events"][0]["payload"]
        assert payload["task_key"] == "T1"
        assert "title" not in payload
        assert "goal" not in payload
        assert "reason" not in payload
        await tracer.close()
    finally:
        db.close()
