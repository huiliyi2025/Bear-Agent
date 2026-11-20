"""Composition root.

All concrete adapters are wired here; the kernel never constructs a database,
provider, sandbox or UI. This is the only place that knows the whole object
graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bear_agent.artifacts.store import ArtifactStore
from bear_agent.audit.service import AuditService
from bear_agent.config.models import Settings
from bear_agent.context.anchors import AnchorLedger
from bear_agent.context.builder import ContextBuilder
from bear_agent.context.compaction import ContextCompactor
from bear_agent.core.clock import SystemClock
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.modes import PermissionMode
from bear_agent.observability.bus import EventBus
from bear_agent.observability.logging import install_redacting_filter
from bear_agent.observability.recorder import EventRecorder
from bear_agent.policy.approvals import ApprovalService
from bear_agent.policy.approver import Approver, AutoApprover
from bear_agent.policy.engine import PermissionEngine
from bear_agent.policy.path_guard import PathGuard
from bear_agent.providers.base import ModelProvider
from bear_agent.providers.registry import create_provider
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
from bear_agent.tools.builtin import create_builtin_tools
from bear_agent.tools.builtin.shell import BashTool
from bear_agent.tools.executor import ToolExecutor
from bear_agent.tools.registry import ToolRegistry


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

    async def close(self) -> None:
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
        )
    except BaseException:
        db.close()
        raise


@dataclass
class Container:
    settings: Settings
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

    async def close(self) -> None:
        try:
            await self.provider.aclose()
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
        model_provider = provider or create_provider(settings, secret_store=secret_store)
        try:
            provider_key = settings.provider().resolve_api_key(secret_store)
        except Exception:
            provider_key = ""
        if provider_key:
            redactor.register(f"provider:{settings.agent.default_provider}", provider_key)
        install_redacting_filter(redactor)
        recorder = EventRecorder(sessions, event_bus, redactor)
        audit = AuditService(db, redactor=redactor, clock=clock, enabled=settings.audit.enabled)

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
