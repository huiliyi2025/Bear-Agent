"""Composition root.

All concrete adapters are wired here; the kernel never constructs a database,
provider, sandbox or UI. This is the only place that knows the whole object
graph.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bear_agent.artifacts.store import ArtifactStore
from bear_agent.audit.service import AuditService
from bear_agent.config.models import MCPConfig, Settings
from bear_agent.context.anchors import AnchorLedger
from bear_agent.context.builder import ContextBuilder
from bear_agent.context.compaction import ContextCompactor
from bear_agent.core.clock import SystemClock
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.modes import PermissionMode
from bear_agent.hooks.events import HookEvent
from bear_agent.hooks.manager import HookManager
from bear_agent.hooks.models import HookRegistration
from bear_agent.mcp.manager import MCPManager
from bear_agent.observability.bus import EventBus
from bear_agent.observability.logging import install_redacting_filter
from bear_agent.observability.metrics.live import LiveMetricsCollector
from bear_agent.observability.metrics.query import MetricsService
from bear_agent.observability.metrics.registry import MetricsRegistry
from bear_agent.observability.recorder import EventRecorder
from bear_agent.observability.trace.service import TraceService
from bear_agent.plugins.api import PluginContext, PluginLoadReport
from bear_agent.plugins.manager import PluginManager
from bear_agent.policy.approvals import ApprovalService
from bear_agent.policy.approver import Approver, AutoApprover
from bear_agent.policy.engine import PermissionEngine
from bear_agent.policy.path_guard import PathGuard
from bear_agent.providers.base import ModelProvider
from bear_agent.providers.router import FallbackProvider, build_fallback_provider
from bear_agent.runtime.model_caller import ModelCaller
from bear_agent.runtime.runtime import AgentRuntime
from bear_agent.runtime.summarizer import ModelSummarizer
from bear_agent.runtime.turn_executor import TurnExecutor
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.secrets.store import SecretManager, secret_store_from_config
from bear_agent.sessions.checkpoints import CheckpointService
from bear_agent.sessions.claims import ClaimService
from bear_agent.sessions.fork import SessionForkService
from bear_agent.sessions.recovery import RecoveryService
from bear_agent.sessions.service import SessionService
from bear_agent.shell.process_registry import ProcessRegistry
from bear_agent.shell.sandbox.base import SandboxManager
from bear_agent.storage.continuity_repositories import (
    AnchorRepository,
    ArtifactRepository,
    CheckpointRepository,
    CompactionRepository,
    ContextStateRepository,
    ProcessRepository,
)
from bear_agent.storage.database import Database
from bear_agent.storage.security_repositories import (
    ApprovalRepository,
    ClaimRepository,
    SecretNameRepository,
)
from bear_agent.tasks.engine import TaskEngine
from bear_agent.tasks.planner import PlanGenerator
from bear_agent.tasks.service import TaskService
from bear_agent.tasks.tools import create_task_tools
from bear_agent.tasks.verifier import ModelTaskVerifier
from bear_agent.tools.builtin import create_builtin_tools
from bear_agent.tools.builtin.shell import BashTool
from bear_agent.tools.executor import ToolExecutor
from bear_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class Maintenance:
    """Offline services for session inspection, recovery and fork.

    These commands must work without a model provider or keyring, so this
    composition root deliberately does not construct a provider, tools or the
    runtime.
    """

    settings: Settings
    db: Database
    sessions: SessionService
    bus: EventBus
    recorder: EventRecorder
    audit: AuditService
    claims: ClaimService
    processes: ProcessRegistry
    checkpoints: CheckpointService
    recovery: RecoveryService
    fork: SessionForkService
    tasks: TaskService
    tracer: TraceService
    metrics: MetricsService

    async def close(self) -> None:
        try:
            await self.tracer.close()
        finally:
            self.db.close()


def build_maintenance(
    settings: Settings,
    *,
    cwd: str | Path,
    bus: EventBus | None = None,
) -> Maintenance:
    data_dir = settings.ensure_data_dirs()
    db = Database(data_dir / "bear.db")
    try:
        clock = SystemClock()
        sessions = SessionService(db, clock)
        event_bus = bus or EventBus()
        redactor = SecretRedactor()
        recorder = EventRecorder(sessions, event_bus, redactor)
        audit = AuditService(db, redactor=redactor, clock=clock, enabled=settings.audit.enabled)
        tracer = TraceService(db, config=settings.observability, redactor=redactor, clock=clock)
        metrics = MetricsService(db, config=settings.observability, clock=clock, settings=settings)
        task_service = TaskService(
            db,
            recorder,
            audit=audit,
            config=settings.tasks,
            clock=clock,
            redactor=redactor,
        )
        anchors = AnchorRepository(db)
        compactions = CompactionRepository(db)
        context_state = ContextStateRepository(db)
        checkpoints = CheckpointService(
            sessions=sessions,
            checkpoints=CheckpointRepository(db),
            compactions=compactions,
            anchors=anchors,
            context_state=context_state,
            recorder=recorder,
            audit=audit,
            config=settings.checkpoints,
            redactor=redactor,
            task_snapshot=task_service.snapshot_sync,
            clock=clock,
        )
        claims = ClaimService(
            ClaimRepository(db),
            PathGuard(cwd),
            clock=clock,
            ttl_seconds=settings.claims.ttl_s,
        )
        processes = ProcessRegistry(
            ProcessRepository(db), clock=clock, recorder=recorder, audit=audit
        )
        recovery = RecoveryService(
            sessions=sessions,
            claims=claims if settings.claims.enabled else None,
            processes=processes,
            checkpoints=checkpoints,
            recorder=recorder,
            audit=audit,
            config=settings.recovery,
            tasks=task_service,
            clock=clock,
        )
        fork = SessionForkService(
            sessions=sessions,
            anchors=anchors,
            recorder=recorder,
            audit=audit,
            clock=clock,
        )
        return Maintenance(
            settings=settings,
            db=db,
            sessions=sessions,
            bus=event_bus,
            recorder=recorder,
            audit=audit,
            claims=claims,
            processes=processes,
            checkpoints=checkpoints,
            recovery=recovery,
            fork=fork,
            tasks=task_service,
            tracer=tracer,
            metrics=metrics,
        )
    except BaseException:
        db.close()
        raise


@dataclass
class Container:
    settings: Settings
    cwd: Path
    db: Database
    sessions: SessionService
    bus: EventBus
    recorder: EventRecorder
    provider: ModelProvider
    registry: ToolRegistry
    path_guard: PathGuard
    policy: PermissionEngine
    approver: Approver
    approvals: ApprovalService
    claims: ClaimService
    sandbox: SandboxManager
    secrets: SecretManager
    redactor: SecretRedactor
    audit: AuditService
    tool_executor: ToolExecutor
    context_builder: ContextBuilder
    model_caller: ModelCaller
    turn_executor: TurnExecutor
    runtime: AgentRuntime
    artifacts: ArtifactStore
    anchors: AnchorLedger
    compactor: ContextCompactor
    checkpoints: CheckpointService
    recovery: RecoveryService
    fork: SessionForkService
    processes: ProcessRegistry
    tasks: TaskService
    planner: PlanGenerator
    task_verifier: ModelTaskVerifier
    task_engine: TaskEngine | None
    hooks: HookManager
    tracer: TraceService
    metrics: MetricsService
    metrics_registry: MetricsRegistry
    mcp: MCPManager | None = None
    plugins: PluginManager | None = None
    plugin_report: PluginLoadReport | None = None
    _hooks_unsubscribe: Any = None
    _metrics_unsubscribe: Any = None
    _started: bool = False
    _closed: bool = False

    async def startup(self) -> None:
        """Load plugins, connect MCP servers and reconcile diagnostics."""
        if self._started:
            return
        with contextlib.suppress(Exception):
            await self.tracer.reconcile_incomplete()
        with contextlib.suppress(Exception):
            await self.tracer.prune()
        report: PluginLoadReport | None = None
        if self.plugins is not None:
            counter = 0

            def hook_factory(**kwargs: Any) -> HookRegistration:
                nonlocal counter
                counter += 1
                kwargs.setdefault("id", f"{kwargs.get('source', 'plugin')}:{counter}")
                raw_events = kwargs.get("events") or []
                kwargs["events"] = frozenset(
                    event if isinstance(event, HookEvent) else HookEvent.from_string(str(event))
                    for event in raw_events
                )
                return HookRegistration(**kwargs)

            context = PluginContext.from_managers(
                workspace=self.cwd,
                hook_manager=self.hooks,
                tool_registry=self.registry,
            )
            context.register_hook_command = getattr(self.hooks, "register_command", None)
            context.hook_registration_factory = hook_factory
            context.reserved_hook_ids = frozenset(
                command.id for command in self.settings.hooks.commands
            )
            context.reserved_mcp_servers = frozenset(self.settings.mcp.servers)
            context.reserved_tools = frozenset(self.registry.names())
            report = await self.plugins.load(context)
            self.plugin_report = report
            for plugin_id, error in report.failed.items():
                logger.warning("plugin %s failed: %s", plugin_id, error)

        servers = dict(self.settings.mcp.servers)
        if report is not None:
            for contribution in report.contributions.values():
                servers.update(contribution.mcp_servers)
        if servers:
            mcp_config = MCPConfig(enabled=self.settings.mcp.enabled, servers=servers)
            manager = MCPManager(
                mcp_config,
                self.registry,
                workspace=self.cwd,
                server_cwd=self.settings.ensure_data_dirs() / "mcp",
                redactor=self.redactor,
                secret_store=self.secrets.store,
            )
            await manager.start()
            self.mcp = manager
        self._started = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.mcp is not None:
                with contextlib.suppress(Exception):
                    await self.mcp.stop()
            if self.plugins is not None:
                with contextlib.suppress(Exception):
                    await self.plugins.unload_all()
            if self._hooks_unsubscribe is not None:
                self._hooks_unsubscribe()
            if self._metrics_unsubscribe is not None:
                self._metrics_unsubscribe()
            with contextlib.suppress(Exception):
                await self.provider.aclose()
        finally:
            try:
                await self.tracer.close()
            finally:
                self.db.close()


def build_container(
    settings: Settings,
    *,
    cwd: str | Path,
    approver: Approver | None = None,
    provider: ModelProvider | None = None,
    bus: EventBus | None = None,
) -> Container:
    data_dir = settings.ensure_data_dirs()
    db = Database(data_dir / "bear.db")
    try:
        clock = SystemClock()
        sessions = SessionService(db, clock)
        event_bus = bus or EventBus()
        redactor = SecretRedactor()
        secret_store = secret_store_from_config(settings.secrets.backend)
        secrets = SecretManager(secret_store, SecretNameRepository(db), redactor)
        install_redacting_filter(redactor)
        recorder = EventRecorder(sessions, event_bus, redactor)
        audit = AuditService(db, redactor=redactor, clock=clock, enabled=settings.audit.enabled)
        tracer = TraceService(db, config=settings.observability, redactor=redactor, clock=clock)
        metrics = MetricsService(db, config=settings.observability, clock=clock, settings=settings)
        metrics_registry = MetricsRegistry()
        if settings.observability.enabled and settings.observability.metrics_enabled:
            live_metrics = LiveMetricsCollector(metrics_registry)
            metrics_unsubscribe = event_bus.subscribe(live_metrics.on_event)
        else:
            metrics_unsubscribe = None

        provider_event_types = {
            "provider.selected": EventType.PROVIDER_SELECTED,
            "provider.retry": EventType.PROVIDER_RETRY,
            "provider.fallback": EventType.PROVIDER_FALLBACK,
            "provider.circuit.open": EventType.PROVIDER_CIRCUIT_OPEN,
            "provider.failed": EventType.PROVIDER_FAILED,
        }

        async def provider_event(event: str, payload: dict[str, object]) -> None:
            # Provider routing events are diagnostic; session-scoped events are
            # recorded when the request carries session metadata. Never log
            # secrets: the router already redacted the payload.
            safe = redactor.redact_obj(payload)
            logger.info("provider event %s: %s", event, safe)
            session_id = safe.get("session_id")
            event_type = provider_event_types.get(event)
            if isinstance(session_id, str) and event_type is not None:
                run_id = safe.get("run_id")
                await recorder.record(
                    session_id,
                    event_type,
                    run_id=run_id if isinstance(run_id, str) else None,
                    payload=safe,
                )

        if provider is not None:
            model_provider = provider
        else:
            model_provider = build_fallback_provider(
                settings,
                secret_store=secret_store,
                on_event=provider_event,
            )
        try:
            provider_key = settings.provider().resolve_api_key(secret_store)
        except Exception:
            provider_key = ""
        if provider_key:
            redactor.register(f"provider:{settings.agent.default_provider}", provider_key)
        if isinstance(model_provider, FallbackProvider):
            for route in model_provider.routes:
                route_config = getattr(route.provider, "config", None)
                try:
                    route_key = (
                        route_config.resolve_api_key(secret_store)
                        if route_config is not None
                        else ""
                    )
                except Exception:
                    continue
                if route_key:
                    redactor.register(f"provider:{route.name}", route_key)
            for issue in model_provider.configuration_errors:
                logger.warning("provider route skipped: %s", redactor.redact_obj(issue))
        task_service = TaskService(
            db,
            recorder,
            audit=audit,
            config=settings.tasks,
            clock=clock,
            redactor=redactor,
        )
        hooks = HookManager(
            config=settings.hooks,
            recorder=recorder,
            audit=audit,
            redactor=redactor,
            workspace=Path(cwd).resolve(),
            safe_cwd=data_dir / "hooks",
        )
        hooks_unsubscribe = event_bus.subscribe(hooks.on_bus_event)
        plugin_manager = PluginManager(
            settings.plugins,
            data_dir=data_dir,
            hooks_allow_external=settings.hooks.allow_external,
            hooks_enabled=settings.hooks.enabled,
        )

        artifact_repository = ArtifactRepository(db)
        checkpoints_repository = CheckpointRepository(db)
        compactions_repository = CompactionRepository(db)
        anchors_repository = AnchorRepository(db)
        context_state_repository = ContextStateRepository(db)
        artifacts = ArtifactStore(
            data_dir / "artifacts",
            artifact_repository,
            redactor=redactor,
        )
        anchor_ledger = AnchorLedger(
            anchors_repository, clock=clock, recorder=recorder, redactor=redactor
        )
        checkpoints = CheckpointService(
            sessions=sessions,
            checkpoints=checkpoints_repository,
            compactions=compactions_repository,
            anchors=anchors_repository,
            context_state=context_state_repository,
            recorder=recorder,
            audit=audit,
            config=settings.checkpoints,
            redactor=redactor,
            task_snapshot=task_service.snapshot_sync,
            clock=clock,
        )
        processes = ProcessRegistry(
            ProcessRepository(db),
            clock=clock,
            recorder=recorder,
            audit=audit,
        )

        path_guard = PathGuard(cwd)
        sandbox = SandboxManager(settings.sandbox, cwd)
        shell_tool = (
            BashTool(
                config=settings.shell,
                workspace=cwd,
                sandbox=sandbox,
                process_registry=processes,
            )
            if settings.shell.enabled
            else None
        )
        registry = ToolRegistry()
        for tool in create_builtin_tools(shell=shell_tool, artifacts=artifacts):
            registry.register(tool)
        if settings.tasks.enabled:
            for tool in create_task_tools(task_service):
                registry.register(tool)

        policy = PermissionEngine(settings.agent.permission_mode)
        chosen_approver = approver or AutoApprover()
        approvals = ApprovalService(
            ApprovalRepository(db),
            chosen_approver,
            clock=clock,
            session_ttl_s=settings.approvals.session_ttl_s,
            project_ttl_s=settings.approvals.project_ttl_s,
        )
        claims = ClaimService(
            ClaimRepository(db),
            path_guard,
            clock=clock,
            ttl_seconds=settings.claims.ttl_s,
        )
        tool_executor = ToolExecutor(
            registry=registry,
            sessions=sessions,
            policy=policy,
            approvals=approvals,
            recorder=recorder,
            path_guard=path_guard,
            audit=audit,
            redactor=redactor,
            claims=claims if settings.claims.enabled else None,
            hooks=hooks,
        )
        context_builder = ContextBuilder(
            cwd=cwd,
            context_window=settings.agent.context_window,
            max_output_tokens=settings.agent.max_output_tokens,
            system_prompt_extra=settings.agent.system_prompt_extra,
        )
        model_caller = ModelCaller(model_provider)
        summarizer = ModelSummarizer(
            model_caller,
            settings.context.summary_model or settings.agent.default_model,
            max_output_tokens=settings.context.l2_max_summary_tokens,
        )
        planner = PlanGenerator(
            model_caller,
            settings.tasks.planner_model or settings.agent.default_model,
            max_tasks=settings.tasks.max_tasks,
            max_retries=settings.tasks.planner_max_retries,
            max_output_tokens=settings.tasks.planner_max_output_tokens,
        )
        task_verifier = ModelTaskVerifier(
            model_caller,
            settings.tasks.verification_model or settings.agent.default_model,
        )
        task_engine = (
            TaskEngine(
                tasks=task_service,
                sessions=sessions,
                verifier=task_verifier,
                config=settings.tasks,
                recorder=recorder,
                audit=audit,
                clock=clock,
            )
            if settings.tasks.enabled
            else None
        )

        async def checkpoint_for_compaction(
            session_id: str,
            fence: LeaseFence,
            kind: str,
        ) -> str | None:
            if not settings.checkpoints.enabled:
                return None
            record = await checkpoints.create(
                session_id,
                fence=fence,
                kind=kind,
            )
            return record.id

        compactor = ContextCompactor(
            sessions=sessions,
            builder=context_builder,
            config=settings.context,
            compactions=compactions_repository,
            context_state=context_state_repository,
            anchors=anchor_ledger,
            artifacts=artifacts,
            recorder=recorder,
            audit=audit,
            summarizer=summarizer if settings.context.l3_enabled else None,
            checkpoint_callback=(
                checkpoint_for_compaction
                if settings.checkpoints.enabled and settings.context.l4_enabled
                else None
            ),
            redactor=redactor,
            clock=clock,
        )
        turn_executor = TurnExecutor(
            sessions=sessions,
            context_builder=context_builder,
            model_caller=model_caller,
            tool_executor=tool_executor,
            registry=registry,
            recorder=recorder,
            compactor=compactor,
            tasks=task_service,
            hooks=hooks,
            model=settings.agent.default_model,
            provider_name=settings.agent.default_provider,
            temperature=settings.agent.temperature,
            max_output_tokens=settings.agent.max_output_tokens,
            stream=settings.agent.stream,
        )
        recovery = RecoveryService(
            sessions=sessions,
            claims=claims if settings.claims.enabled else None,
            processes=processes,
            checkpoints=checkpoints,
            recorder=recorder,
            audit=audit,
            config=settings.recovery,
            tasks=task_service,
            clock=clock,
        )
        runtime = AgentRuntime(
            sessions=sessions,
            turn_executor=turn_executor,
            recorder=recorder,
            max_turns=settings.agent.max_turns,
            max_tool_calls=settings.agent.max_tool_calls,
            max_run_seconds=settings.agent.max_run_seconds,
            claims=claims if settings.claims.enabled else None,
            recovery=recovery,
            checkpoints=checkpoints if settings.checkpoints.enabled else None,
            checkpoint_interval=settings.checkpoints.interval_turns,
            task_engine=task_engine,
            task_max_turns=settings.tasks.max_task_turns,
            tracer=tracer,
        )
        fork = SessionForkService(
            sessions=sessions,
            anchors=anchors_repository,
            recorder=recorder,
            audit=audit,
            clock=clock,
        )
        return Container(
            settings=settings,
            cwd=Path(cwd).resolve(),
            db=db,
            sessions=sessions,
            bus=event_bus,
            recorder=recorder,
            provider=model_provider,
            registry=registry,
            path_guard=path_guard,
            policy=policy,
            approver=chosen_approver,
            approvals=approvals,
            claims=claims,
            sandbox=sandbox,
            secrets=secrets,
            redactor=redactor,
            audit=audit,
            tool_executor=tool_executor,
            context_builder=context_builder,
            model_caller=model_caller,
            turn_executor=turn_executor,
            runtime=runtime,
            artifacts=artifacts,
            anchors=anchor_ledger,
            compactor=compactor,
            checkpoints=checkpoints,
            recovery=recovery,
            fork=fork,
            processes=processes,
            tasks=task_service,
            planner=planner,
            task_verifier=task_verifier,
            task_engine=task_engine,
            hooks=hooks,
            tracer=tracer,
            metrics=metrics,
            metrics_registry=metrics_registry,
            plugins=plugin_manager,
            _hooks_unsubscribe=hooks_unsubscribe,
            _metrics_unsubscribe=metrics_unsubscribe,
        )
    except BaseException:
        db.close()
        raise


def permission_mode(value: str) -> PermissionMode:
    try:
        return PermissionMode(value)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in PermissionMode)
        raise ValueError(f"unknown permission mode {value!r}; choose {allowed}") from exc
