"""Bear-Agent CLI.

Commands are thin: load settings, build the composition root, subscribe a
renderer, call the runtime. No agent logic lives in the CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from bear_agent import __version__
from bear_agent.audit.service import AuditEntry, AuditService, AuditVerification
from bear_agent.benchmarks import (
    BenchmarkRunner,
    BenchmarkSkipped,
    build_registry,
    parse_params,
)
from bear_agent.bootstrap import (
    Container,
    build_container,
    build_maintenance,
    permission_mode,
)
from bear_agent.config.loader import (
    default_config_path,
    load_settings,
    settings_to_toml,
    write_default_config,
)
from bear_agent.config.models import Settings
from bear_agent.core.cancel import CancelToken
from bear_agent.core.errors import BearError, CheckpointError, TaskStateError
from bear_agent.core.fencing import LeaseFence, reset_fence, set_fence
from bear_agent.core.jsonio import write_json
from bear_agent.core.messages import SystemMessage, UserMessage
from bear_agent.core.usage import TokenUsage
from bear_agent.diagnostics import (
    CheckMode,
    CheckStatus,
    DiagnosticCheck,
    DiagnosticReport,
    DiagnosticsService,
)
from bear_agent.hooks.manager import HookManager
from bear_agent.mcp.manager import MCPManager
from bear_agent.observability.metrics import MetricsService
from bear_agent.observability.trace import TraceService
from bear_agent.plugins.manager import PluginManager
from bear_agent.policy.approver import AutoApprover, DenyingApprover
from bear_agent.providers.base import ModelRequest
from bear_agent.providers.router import build_fallback_provider
from bear_agent.runtime.model_caller import ModelCaller
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.secrets.store import SecretManager, secret_store_from_config
from bear_agent.sessions.models import SessionRecord
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database
from bear_agent.storage.migrations import SCHEMA_VERSION
from bear_agent.storage.security_repositories import SecretNameRepository
from bear_agent.tasks.models import PlanDraft, TaskStatus
from bear_agent.tasks.service import TaskService
from bear_agent.tools.registry import ToolRegistry
from bear_agent.ui.cli.approvals import CliApprover
from bear_agent.ui.cli.render import Renderer


def _configure_stdout_utf8() -> None:
    """让 Typer/Rich 在 Windows 终端正确显示中文。"""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(OSError, ValueError):
            reconfigure(encoding="utf-8", errors="replace")


_configure_stdout_utf8()
console = Console()
app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
    help="Bear-Agent——本地优先、可恢复、可扩展的 Agent Harness。",
)
sessions_app = typer.Typer(help="查看与恢复会话。")
tasks_app = typer.Typer(help="规划并执行多步骤任务。")
config_app = typer.Typer(help="查看配置。")
audit_app = typer.Typer(help="查看安全审计链。")
secret_app = typer.Typer(help="管理 keyring 密钥。")
providers_app = typer.Typer(help="查看与测试模型 Provider / Fallback。")
plugins_app = typer.Typer(help="管理 Plugin。")
mcp_app = typer.Typer(help="查看 MCP Server 与工具。")
hooks_app = typer.Typer(help="查看 Hook。")
trace_app = typer.Typer(help="查看、查询与导出 Agent Trace。")
metrics_app = typer.Typer(help="查看与导出运行指标。")
bench_app = typer.Typer(help="运行、保存与比较 Benchmark。")
app.add_typer(sessions_app, name="sessions")
app.add_typer(tasks_app, name="tasks")
app.add_typer(config_app, name="config")
app.add_typer(audit_app, name="audit")
app.add_typer(secret_app, name="secret")
app.add_typer(providers_app, name="providers")
app.add_typer(plugins_app, name="plugins")
app.add_typer(mcp_app, name="mcp")
app.add_typer(hooks_app, name="hooks")
app.add_typer(trace_app, name="trace")
app.add_typer(metrics_app, name="metrics")
app.add_typer(bench_app, name="bench")


# --------------------------------------------------------------------- helpers


def _load(
    *,
    config: Path | None,
    base_url: str | None,
    api_key: str | None,
    provider: str | None,
    model: str | None,
    permission: str | None,
    max_turns: int | None,
) -> Settings:
    overrides: dict[str, Any] = {"agent": {}, "providers": {}}
    if provider:
        overrides["agent"]["default_provider"] = provider
    if model:
        overrides["agent"]["default_model"] = model
    if permission:
        try:
            overrides["agent"]["permission_mode"] = permission_mode(permission).value
        except ValueError as exc:
            _fail(str(exc), code=2)
    if max_turns is not None:
        if max_turns <= 0:
            _fail("--max-turns must be positive", code=2)
        overrides["agent"]["max_turns"] = max_turns
    if base_url or api_key:
        name = provider or "default"
        overrides["providers"][name] = {
            "type": "openai_compat",
            "base_url": base_url or "https://api.openai.com/v1",
            "api_key": api_key or "",
            "api_key_env": None if api_key else "OPENAI_API_KEY",
            "models": [],
        }
        overrides["agent"]["default_provider"] = name
    try:
        return load_settings(config, overrides=overrides)
    except BearError as exc:
        _fail(str(exc), code=2)
    except Exception as exc:
        _fail(str(exc), code=2)
    raise AssertionError("unreachable")


def _fail(message: str, *, code: int = 1) -> NoReturn:
    message = SecretRedactor().redact(message)
    console.print(f"[red]error:[/red] {message}", highlight=False)
    raise typer.Exit(code=code)


def _make_approver(*, interactive: bool, yes: bool) -> Any:
    if yes:
        return AutoApprover()
    if interactive:
        return CliApprover(console)
    return DenyingApprover()


def _resolve_cwd(cwd: Path | None) -> Path:
    target = (cwd or Path.cwd()).resolve()
    if not target.is_dir():
        _fail(f"working directory does not exist: {target}", code=2)
    return target


async def _open_session(
    container: Container,
    cwd: Path,
    *,
    resume: str | None,
    continue_latest: bool,
) -> SessionRecord:
    if resume:
        session = await container.sessions.get(resume)
        if session is None:
            raise BearError(f"session not found: {resume}")
        return session
    if continue_latest:
        session = await container.sessions.latest(cwd)
        if session is not None:
            return session
    return await container.sessions.create(
        cwd=cwd,
        model=container.settings.agent.default_model,
        provider=container.settings.agent.default_provider,
        publish=container.bus.publish,
    )


async def _sessions_db(settings: Settings) -> tuple[Database, SessionService]:
    data_dir = settings.ensure_data_dirs()
    db = Database(data_dir / "bear.db")
    return db, SessionService(db)


def _usage_dict(usage: TokenUsage) -> dict[str, int]:
    return usage.model_dump()


def _print_banner(settings: Settings, cwd: Path, session: SessionRecord) -> None:
    provider = settings.agent.default_provider
    console.print(
        Panel(
            f"[bold]Bear-Agent[/bold] [dim]v{__version__}[/dim]\n"
            f"model    [cyan]{settings.agent.default_model}[/cyan]  "
            f"provider [cyan]{provider}[/cyan]\n"
            f"cwd      {cwd}\n"
            f"session  [dim]{session.id}[/dim]\n"
            f"mode     {settings.agent.permission_mode.value}",
            border_style="cyan",
        )
    )
    console.print("[dim]Type a task, /help for commands, /exit to quit.[/dim]")


HELP = """\
/help                 show this help
/tools                list registered tools
/metrics              show live counters/histograms for this process
/session              show current session id and status
/permission <mode>    plan | supervised | auto | unrestricted
/new                  start a new session
/compact              force layered context compaction now
/anchor <text>        pin a fact that compaction must keep
/anchors              list active anchors
/fork [message_seq]   fork this session at a safe position
/plan <goal>          generate a structured plan for a complex goal
/tasks                show plan, todo and task status
/run-plan             execute the active plan
/task <task_id>       show one task and its attempts
/cancel-plan          cancel the active plan and unfinished tasks
/exit                 quit
"""


# ----------------------------------------------------------------------- chat


@app.command()
def chat(
    config: Path | None = typer.Option(None, "--config", help="Config TOML path."),
    cwd: Path | None = typer.Option(None, "--cwd", "-C", help="Workspace directory."),
    provider: str | None = typer.Option(None, "--provider", help="Provider name."),
    model: str | None = typer.Option(None, "--model", help="Model id."),
    base_url: str | None = typer.Option(None, "--base-url", help="OpenAI-compatible URL."),
    api_key: str | None = typer.Option(None, "--api-key", help="API key (prefer env)."),
    permission: str | None = typer.Option(None, "--permission", help="Permission mode."),
    max_turns: int | None = typer.Option(None, "--max-turns", help="Turn budget."),
    resume: str | None = typer.Option(None, "--resume", "-r", help="Resume session id."),
    continue_latest: bool = typer.Option(
        False, "--continue", "-c", help="Resume the latest session for this cwd."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve tool prompts."),
) -> None:
    """启动交互式会话。"""
    settings = _load(
        config=config,
        base_url=base_url,
        api_key=api_key,
        provider=provider,
        model=model,
        permission=permission,
        max_turns=max_turns,
    )
    workspace = _resolve_cwd(cwd)
    try:
        asyncio.run(
            _chat(
                settings,
                workspace,
                resume=resume,
                continue_latest=continue_latest,
                yes=yes,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[dim]bye[/dim]")


async def _chat(
    settings: Settings,
    cwd: Path,
    *,
    resume: str | None,
    continue_latest: bool,
    yes: bool,
) -> None:
    try:
        settings.validate_ready()
    except ValueError as exc:
        _fail(str(exc), code=2)
    try:
        container = build_container(
            settings, cwd=cwd, approver=_make_approver(interactive=True, yes=yes)
        )
    except Exception as exc:
        _fail(f"failed to initialize Bear-Agent: {exc}", code=1)
        raise AssertionError("unreachable") from exc
    with contextlib.suppress(Exception):
        await container.secrets.hydrate_redactor()
    unsubscribe = container.bus.subscribe(Renderer(console=console))
    try:
        try:
            await container.startup()
        except Exception as exc:
            _fail(f"failed to start extensions: {exc}", code=1)
        try:
            session = await _open_session(
                container, cwd, resume=resume, continue_latest=continue_latest
            )
        except BearError as exc:
            _fail(str(exc), code=2)
            return
        _print_banner(settings, cwd, session)
        while True:
            try:
                line = await asyncio.to_thread(console.input, "[bold cyan]bear> [/]")
            except (EOFError, KeyboardInterrupt):
                break
            text = line.strip()
            if not text:
                continue
            if text.startswith("/"):
                if text in ("/exit", "/quit"):
                    break
                if text in ("/help", "/h"):
                    console.print(HELP)
                    continue
                if text == "/tools":
                    for name in container.registry.names():
                        tool = container.registry.get(name)
                        assert tool is not None
                        console.print(
                            f"  [cyan]{name}[/cyan] "
                            f"[dim]({tool.side_effect.value})[/dim] {tool.description}"
                        )
                    continue
                if text == "/metrics":
                    snapshot = container.metrics_registry.snapshot()
                    if not snapshot:
                        console.print("[dim]no live metrics yet[/dim]")
                        continue
                    for name in sorted(snapshot):
                        family = snapshot[name]
                        total = 0.0
                        if family["kind"] == "histogram":
                            total = sum(
                                int(item["histogram"]["count"]) for item in family["series"]
                            )
                        else:
                            total = sum(float(item["value"]) for item in family["series"])
                        console.print(f"  [cyan]{name}[/cyan] {total:g}")
                    continue
                if text == "/session":
                    session = await container.sessions.get(session.id) or session
                    console.print(
                        f"session [dim]{session.id}[/dim] status={session.status} "
                        f"last_seq={session.last_seq}"
                    )
                    continue
                if text.startswith("/permission"):
                    parts = text.split()
                    if len(parts) != 2:
                        console.print("usage: /permission plan|supervised|auto|unrestricted")
                        continue
                    try:
                        mode = permission_mode(parts[1])
                    except ValueError as exc:
                        console.print(f"[red]{exc}[/red]")
                        continue
                    container.policy.set_mode(mode)
                    console.print(f"permission mode: {mode.value}")
                    continue
                if text == "/new":
                    session = await container.sessions.create(
                        cwd=cwd,
                        model=settings.agent.default_model,
                        provider=settings.agent.default_provider,
                        publish=container.bus.publish,
                    )
                    console.print(f"new session [dim]{session.id}[/dim]")
                    continue
                if text == "/anchors":
                    for anchor in await container.anchors.active(session.id):
                        console.print(
                            f"  [cyan]{anchor.id}[/cyan] [{anchor.kind}] {anchor.text[:200]}"
                        )
                    continue
                if text.startswith("/anchor "):
                    value = text[len("/anchor ") :].strip()
                    if not value:
                        console.print("usage: /anchor <text>")
                        continue
                    owner = f"cli-{os.getpid()}"
                    try:
                        epoch = await container.sessions.acquire_lease(session.id, owner, 60.0)
                    except BearError as exc:
                        console.print(f"[red]{exc}[/red]")
                        continue
                    fence = LeaseFence(owner, epoch)
                    token = set_fence(fence)
                    try:
                        anchor = await container.anchors.add(
                            session.id, value, kind="manual", fence=fence
                        )
                    finally:
                        reset_fence(token)
                        with contextlib.suppress(Exception):
                            await container.sessions.release_lease(session.id, owner, epoch)
                    console.print(f"anchor [dim]{anchor.id}[/dim] pinned")
                    continue
                if text == "/compact":
                    owner = f"cli-{os.getpid()}"
                    try:
                        epoch = await container.sessions.acquire_lease(session.id, owner, 900.0)
                    except BearError as exc:
                        console.print(f"[red]{exc}[/red]")
                        continue
                    fence = LeaseFence(owner, epoch)
                    token = set_fence(fence)
                    try:
                        messages = await container.sessions.load_messages(session.id)
                        prepared = await container.compactor.force(
                            session,
                            messages,
                            container.registry.specs(),
                            fence=fence,
                            turn_index=0,
                        )
                    except Exception as exc:
                        console.print(f"[red]compaction failed:[/red] {exc}")
                        continue
                    finally:
                        reset_fence(token)
                        with contextlib.suppress(Exception):
                            await container.sessions.release_lease(session.id, owner, epoch)
                    console.print(
                        f"compacted levels={prepared.levels or 'none'} "
                        f"tokens {prepared.before_tokens} -> {prepared.estimated_tokens}"
                    )
                    for note in prepared.notes:
                        console.print(f"  [dim]{note}[/dim]")
                    continue
                if text == "/tasks":
                    await _print_plan(container.tasks, session.id, attempts=True)
                    continue
                if text.startswith("/plan "):
                    goal = text[len("/plan ") :].strip()
                    if not goal:
                        console.print("usage: /plan <goal>")
                        continue
                    if await container.tasks.get_plan(session.id) is not None:
                        console.print("this session already has a plan; use /tasks")
                        continue
                    owner = f"cli-{os.getpid()}"
                    try:
                        epoch = await container.sessions.acquire_lease(session.id, owner, 120.0)
                    except BearError as exc:
                        console.print(f"[red]{exc}[/red]")
                        continue
                    fence = LeaseFence(owner, epoch)
                    token = set_fence(fence)
                    try:
                        try:
                            draft = await container.planner.generate(
                                goal, context=f"Workspace: {cwd}"
                            )
                            created = await container.tasks.create_plan(
                                session.id, draft, source="model", fence=fence
                            )
                        except Exception as exc:
                            console.print(f"[red]plan generation failed:[/red] {exc}")
                            continue
                        if session.title is None:
                            await container.sessions.set_title(
                                session.id,
                                " ".join(goal.split())[:80] or "Task plan",
                                fence=fence,
                            )
                    finally:
                        reset_fence(token)
                        with contextlib.suppress(Exception):
                            await container.sessions.release_lease(session.id, owner, epoch)
                    console.print(
                        f"plan [cyan]{created.id}[/cyan] v{created.version} "
                        f"with {len(draft.tasks)} task(s)"
                    )
                    await _print_plan(container.tasks, session.id)
                    continue
                if text == "/run-plan":
                    try:
                        report = await container.runtime.run_plan(session.id)
                    except BearError as exc:
                        console.print(f"[red]plan run failed:[/red] {exc}")
                        continue
                    await _print_plan(container.tasks, session.id)
                    console.print(
                        f"[bold]{report.status}[/bold] | run={report.tasks_run} "
                        f"completed={report.tasks_completed} failed={report.tasks_failed} "
                        f"blocked={report.tasks_blocked} revisions={report.revisions}"
                    )
                    for issue in report.issues:
                        console.print(f"[yellow]issue:[/yellow] {issue}")
                    continue
                if text.startswith("/task "):
                    task_id = text[len("/task ") :].strip()
                    task = await container.tasks.get_task(task_id)
                    if task is None:
                        console.print(f"[red]task not found:[/red] {task_id}")
                        continue
                    deps = await container.tasks.dependencies(task_id)
                    console.print(
                        f"[cyan]{task.task_key}[/cyan] {task.title}\n"
                        f"status={task.status.value} attempts={task.attempt_count}/"
                        f"{task.max_attempts} deps={', '.join(deps) or '(none)'}\n"
                        f"acceptance: {task.acceptance_criteria or '(none)'}\n"
                        f"result: {task.result_summary or '(none)'}\n"
                        f"last error: {task.last_error or '(none)'}"
                    )
                    continue
                if text == "/cancel-plan":
                    owner = f"cli-{os.getpid()}"
                    try:
                        epoch = await container.sessions.acquire_lease(session.id, owner, 60.0)
                    except BearError as exc:
                        console.print(f"[red]{exc}[/red]")
                        continue
                    fence = LeaseFence(owner, epoch)
                    token = set_fence(fence)
                    try:
                        plan = await container.tasks.cancel_plan(
                            session.id, reason="cancelled by user", fence=fence
                        )
                    finally:
                        reset_fence(token)
                        with contextlib.suppress(Exception):
                            await container.sessions.release_lease(session.id, owner, epoch)
                    console.print("plan cancelled" if plan else "no plan to cancel")
                    continue
                if text == "/fork" or text.startswith("/fork "):
                    at: int | None = None
                    if text.startswith("/fork "):
                        raw = text[len("/fork ") :].strip()
                        try:
                            at = int(raw)
                        except ValueError:
                            console.print("usage: /fork [message_seq]")
                            continue
                    try:
                        async with container.tracer.trace(
                            "sessions.fork",
                            kind="fork",
                            session_id=session.id,
                            attributes={"at_message_seq": at},
                        ) as fork_scope:
                            forked = await container.fork.fork(
                                session.id,
                                at_message_seq=at,
                                checkpoints=container.checkpoints,
                            )
                            if fork_scope is not None:
                                fork_scope.set_attribute("session_id", forked.session.id)
                    except BearError as exc:
                        console.print(f"[red]fork failed:[/red] {exc}")
                        continue
                    session = forked.session
                    console.print(
                        f"forked [cyan]{session.id}[/cyan] at message {forked.cut_message_seq}"
                    )
                    continue
                console.print(f"[red]unknown command:[/red] {text}")
                continue
            try:
                result = await container.runtime.run(session.id, text)
            except KeyboardInterrupt:
                container.runtime.cancel(session.id)
                console.print("\n[yellow]cancelling run...[/yellow]")
                continue
            except BearError as exc:
                console.print(f"[red]{type(exc).__name__}:[/red] {exc}")
                continue
            except Exception as exc:
                console.print(f"[red]internal error:[/red] {type(exc).__name__}: {exc}")
                continue
            console.print()
            if result.status != RunStatus.COMPLETED:
                console.print(f"[yellow]{result.status.value}: {result.stop_reason}[/yellow]")
    finally:
        unsubscribe()
        await container.close()


# ------------------------------------------------------------------------ run


@app.command()
def run(
    prompt: str = typer.Argument(..., help="Task or question for the agent."),
    config: Path | None = typer.Option(None, "--config", help="Config TOML path."),
    cwd: Path | None = typer.Option(None, "--cwd", "-C", help="Workspace directory."),
    provider: str | None = typer.Option(None, "--provider", help="Provider name."),
    model: str | None = typer.Option(None, "--model", help="Model id."),
    base_url: str | None = typer.Option(None, "--base-url", help="OpenAI-compatible URL."),
    api_key: str | None = typer.Option(None, "--api-key", help="API key (prefer env)."),
    permission: str | None = typer.Option(None, "--permission", help="Permission mode."),
    max_turns: int | None = typer.Option(None, "--max-turns", help="Turn budget."),
    resume: str | None = typer.Option(None, "--resume", "-r", help="Resume session id."),
    json_output: bool = typer.Option(False, "--json", help="Emit NDJSON events."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve tool prompts."),
) -> None:
    """无头执行一个任务。"""
    settings = _load(
        config=config,
        base_url=base_url,
        api_key=api_key,
        provider=provider,
        model=model,
        permission=permission,
        max_turns=max_turns,
    )
    workspace = _resolve_cwd(cwd)
    code = asyncio.run(
        _run_once(
            settings,
            workspace,
            prompt=prompt,
            resume=resume,
            json_output=json_output,
            yes=yes,
        )
    )
    raise typer.Exit(code=code)


async def _run_once(
    settings: Settings,
    cwd: Path,
    *,
    prompt: str,
    resume: str | None,
    json_output: bool,
    yes: bool,
) -> int:
    try:
        settings.validate_ready()
    except ValueError as exc:
        _fail(str(exc), code=2)
    interactive = sys.stdin.isatty() and not json_output
    try:
        container = build_container(
            settings,
            cwd=cwd,
            approver=_make_approver(interactive=interactive, yes=yes),
        )
    except Exception as exc:
        _fail(f"failed to initialize Bear-Agent: {exc}", code=1)
        raise AssertionError("unreachable") from exc
    with contextlib.suppress(Exception):
        await container.secrets.hydrate_redactor()
    renderer = Renderer(json_mode=json_output, console=console)
    unsubscribe = container.bus.subscribe(renderer)
    try:
        try:
            await container.startup()
        except Exception as exc:
            _fail(f"failed to start extensions: {exc}", code=1)
        try:
            session = await _open_session(container, cwd, resume=resume, continue_latest=False)
        except BearError as exc:
            _fail(str(exc), code=2)
            return 2
        try:
            result = await container.runtime.run(session.id, prompt)
        except BearError as exc:
            console.print(f"[red]{type(exc).__name__}:[/red] {container.redactor.redact(str(exc))}")
            return 1
        renderer.summary(
            result.status.value,
            result.turns,
            _usage_dict(result.usage),
            result.stop_reason,
        )
        if result.error:
            console.print(f"[red]{container.redactor.redact(result.error)}[/red]")
        return 0 if result.status == RunStatus.COMPLETED else 1
    finally:
        unsubscribe()
        await container.close()


# -------------------------------------------------------------------- sessions


@sessions_app.command("list")
def sessions_list(
    config: Path | None = typer.Option(None, "--config"),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
) -> None:
    """列出最近会话。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _list() -> list[SessionRecord]:
        db, service = await _sessions_db(settings)
        try:
            return await service.list_recent(limit)
        finally:
            db.close()

    records = asyncio.run(_list())
    table = Table(title="Sessions")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("updated", style="dim")
    table.add_column("status")
    table.add_column("title")
    for record in records:
        table.add_row(
            record.id,
            record.updated_at.strftime("%Y-%m-%d %H:%M"),
            record.status,
            record.title or "",
        )
    console.print(table)


@sessions_app.command("show")
def sessions_show(
    session_id: str = typer.Argument(..., help="Session id."),
    config: Path | None = typer.Option(None, "--config"),
    messages: int = typer.Option(10, "--messages", min=1, max=100),
) -> None:
    """查看会话转录与用量。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _show() -> None:
        db, service = await _sessions_db(settings)
        try:
            record = await service.get(session_id)
            if record is None:
                _fail(f"session not found: {session_id}", code=2)
                return
            lineage = ""
            if record.parent_session_id:
                lineage = (
                    f"\nparent  {record.parent_session_id}"
                    f"\nfork at {record.fork_message_seq}"
                    + (
                        f"\nfork ckpt {record.fork_checkpoint_id}"
                        if record.fork_checkpoint_id
                        else ""
                    )
                )
            console.print(
                Panel(
                    f"id      {record.id}\n"
                    f"cwd     {record.cwd}\n"
                    f"status  {record.status}\n"
                    f"model   {record.model}\n"
                    f"title   {record.title}\n"
                    f"seq     {record.last_seq}"
                    f"{lineage}",
                    title="Session",
                )
            )
            history = await service.load_messages(session_id)
            limit = int(messages)
            for message in history[-limit:]:
                text = message.text_content()
                if message.role == "tool":
                    result = message.tool_result()
                    text = result.content if result else ""
                text = " ".join(text.split())
                console.print(
                    f"[bold]{message.role}[/bold]: {text[:500]}" + ("…" if len(text) > 500 else "")
                )
            usage = await service.usage_summary(session_id)
            console.print(
                f"[dim]usage: {usage.calls} calls | "
                f"{usage.usage.input_tokens} in / {usage.usage.output_tokens} out[/dim]"
            )
        finally:
            db.close()

    asyncio.run(_show())


@sessions_app.command("fork")
def sessions_fork(
    session_id: str = typer.Argument(..., help="Source session id."),
    config: Path | None = typer.Option(None, "--config"),
    at: int | None = typer.Option(
        None, "--at", min=0, help="Copy messages up to this message sequence."
    ),
    checkpoint: str | None = typer.Option(
        None, "--checkpoint", help="Fork from this checkpoint's message position."
    ),
    title: str | None = typer.Option(None, "--title", help="Title for the new session."),
) -> None:
    """从历史位置或 Checkpoint 派生一个独立会话。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _fork() -> None:
        maintenance = build_maintenance(settings, cwd=Path.cwd())
        try:
            async with maintenance.tracer.trace(
                "sessions.fork",
                kind="fork",
                session_id=session_id,
                attributes={
                    "at_message_seq": at,
                    "checkpoint_id": checkpoint,
                },
            ) as fork_scope:
                result = await maintenance.fork.fork(
                    session_id,
                    at_message_seq=at,
                    checkpoint_id=checkpoint,
                    title=title,
                    checkpoints=maintenance.checkpoints,
                )
                if fork_scope is not None:
                    fork_scope.set_attribute("session_id", result.session.id)
            console.print(
                f"forked [cyan]{result.session.id}[/cyan] "
                f"from {session_id} at message {result.cut_message_seq}"
            )
            console.print(f"title: {result.session.title or '(none)'}")
            console.print(f"continue with: bear chat --resume {result.session.id}")
        finally:
            await maintenance.close()

    try:
        asyncio.run(_fork())
    except BearError as exc:
        _fail(str(exc), code=2)
    except Exception as exc:
        _fail(f"fork failed: {exc}", code=1)


@sessions_app.command("recover")
def sessions_recover(
    session_id: str = typer.Argument(..., help="Session id."),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """在持有租约的前提下执行一次崩溃恢复检查。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _recover() -> bool:
        maintenance = build_maintenance(settings, cwd=Path.cwd())
        try:
            record = await maintenance.sessions.get(session_id)
            if record is None:
                _fail(f"session not found: {session_id}", code=2)
            owner = f"cli-recovery-{os.getpid()}"
            try:
                epoch = await maintenance.sessions.acquire_lease(session_id, owner, 120.0)
            except BearError as exc:
                _fail(f"cannot recover while another process holds the lease: {exc}", code=1)
            fence = LeaseFence(owner, epoch)
            token = set_fence(fence)
            try:
                async with maintenance.tracer.trace(
                    "sessions.recover",
                    kind="recovery",
                    session_id=session_id,
                    attributes={"owner": owner, "epoch": epoch},
                ) as recovery_scope:
                    report = await maintenance.recovery.recover_if_needed(session_id, fence=fence)
                    if recovery_scope is not None:
                        recovery_scope.set_attributes(
                            {
                                "recovered": report.recovered,
                                "blocked": report.blocked,
                                "running_tools": report.running_tools,
                                "issues": len(report.issues),
                            }
                        )
            finally:
                reset_fence(token)
                with contextlib.suppress(Exception):
                    await maintenance.sessions.release_lease(session_id, owner, epoch)
            if not report.recovered:
                console.print("no recovery needed")
                return True
            console.print(
                f"[green]recovered[/green] tools={report.running_tools} "
                f"synthesized={len(report.synthesized_tools)} "
                f"claims={report.released_claims} "
                f"processes={report.terminated_processes}"
            )
            if report.checkpoint_id:
                console.print(f"checkpoint: [dim]{report.checkpoint_id}[/dim]")
            for action in report.actions:
                console.print(f"  - {action}")
            for issue in report.issues:
                console.print(f"[yellow]issue:[/yellow] {issue}")
            if report.blocked:
                console.print(
                    "[red]session is blocked for new turns;[/red] inspect it or fork "
                    "from an earlier checkpoint/message."
                )
            return not report.issues
        finally:
            await maintenance.close()

    try:
        ok = asyncio.run(_recover())
    except typer.Exit:
        raise
    except BearError as exc:
        _fail(str(exc), code=1)
    except Exception as exc:
        _fail(f"recovery failed: {exc}", code=1)
    if not ok:
        raise typer.Exit(code=1)


@sessions_app.command("checkpoints")
def sessions_checkpoints(
    session_id: str = typer.Argument(..., help="Session id."),
    config: Path | None = typer.Option(None, "--config"),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
) -> None:
    """列出并校验会话的 Checkpoint。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _list() -> None:
        maintenance = build_maintenance(settings, cwd=Path.cwd())
        try:
            record = await maintenance.sessions.get(session_id)
            if record is None:
                _fail(f"session not found: {session_id}", code=2)
            rows = await maintenance.checkpoints.list(session_id, limit)
            table = Table(title="Checkpoints")
            table.add_column("id", style="cyan", no_wrap=True)
            table.add_column("kind")
            table.add_column("event")
            table.add_column("message")
            table.add_column("created")
            table.add_column("integrity")
            for row in rows:
                try:
                    await maintenance.checkpoints.validate(row)
                    state = "[green]ok[/green]"
                except CheckpointError as exc:
                    state = f"[red]{type(exc).__name__}[/red]"
                except Exception:
                    state = "[red]invalid[/red]"
                table.add_row(
                    row.id,
                    row.kind,
                    str(row.event_seq),
                    str(row.message_seq),
                    row.created_at.strftime("%Y-%m-%d %H:%M"),
                    state,
                )
            console.print(table)
        finally:
            await maintenance.close()

    try:
        asyncio.run(_list())
    except typer.Exit:
        raise
    except BearError as exc:
        _fail(str(exc), code=2)
    except Exception as exc:
        _fail(f"cannot list checkpoints: {exc}", code=1)


# ----------------------------------------------------------------------- tasks


async def _print_plan(tasks: TaskService, session_id: str, *, attempts: bool = False) -> None:
    plan = await tasks.get_plan(session_id)
    if plan is None:
        console.print("[yellow]no plan for this session[/yellow]")
        return
    console.print(
        Panel(
            f"plan     [dim]{plan.id}[/dim]\n"
            f"version  v{plan.version}\n"
            f"status   {plan.status.value}\n"
            f"goal     {plan.goal}",
            title="Plan",
            border_style="cyan",
        )
    )
    todo = await tasks.todo(session_id)
    table = Table(title="Todo")
    table.add_column("", width=3)
    table.add_column("key", style="cyan", no_wrap=True)
    table.add_column("title")
    table.add_column("status")
    table.add_column("deps", style="dim")
    for item in todo:
        table.add_row(
            item.marker,
            item.key,
            item.title,
            item.status.value,
            ", ".join(item.dependencies),
        )
    console.print(table)
    if attempts:
        for item in todo:
            if item.status in {TaskStatus.IN_PROGRESS, TaskStatus.FAILED, TaskStatus.BLOCKED}:
                history = await tasks.attempts(item.task_id)
                for attempt in history[-3:]:
                    console.print(
                        f"  [dim]{item.key} attempt {attempt.attempt_no}: "
                        f"{attempt.status.value}"
                        + (f" — {attempt.error_message}" if attempt.error_message else "")
                        + "[/dim]"
                    )


async def _execute_plan(
    settings: Settings,
    workspace: Path,
    session_id: str,
    *,
    json_output: bool,
    yes: bool = False,
) -> int:
    if not settings.tasks.enabled:
        _fail("task engine is disabled in configuration ([tasks].enabled=false)", code=2)
    try:
        settings.validate_ready()
    except ValueError as exc:
        _fail(str(exc), code=2)
    try:
        container = build_container(
            settings,
            cwd=workspace,
            approver=_make_approver(interactive=sys.stdin.isatty() and not json_output, yes=yes),
        )
    except Exception as exc:
        _fail(f"failed to initialize Bear-Agent: {exc}", code=1)
        raise AssertionError("unreachable") from exc
    renderer = Renderer(json_mode=json_output, console=console)
    unsubscribe = container.bus.subscribe(renderer)
    try:
        try:
            await container.startup()
        except Exception as exc:
            console.print(
                f"[red]failed to start extensions:[/red] {container.redactor.redact(str(exc))}"
            )
            return 1
        try:
            report = await container.runtime.run_plan(session_id)
        except ValueError as exc:
            console.print(f"[red]invalid session:[/red] {container.redactor.redact(str(exc))}")
            return 2
        if json_output:
            print(
                json.dumps(
                    {
                        "type": "plan_result",
                        "status": report.status,
                        "plan_id": report.plan_id,
                        "tasks_run": report.tasks_run,
                        "tasks_completed": report.tasks_completed,
                        "tasks_failed": report.tasks_failed,
                        "tasks_blocked": report.tasks_blocked,
                        "revisions": report.revisions,
                        "reason": report.reason,
                        "issues": report.issues,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        else:
            await _print_plan(container.tasks, session_id)
            console.print(
                f"[bold]{report.status}[/bold] | tasks run={report.tasks_run} "
                f"completed={report.tasks_completed} failed={report.tasks_failed} "
                f"blocked={report.tasks_blocked} revisions={report.revisions}"
                + (f" | {report.reason}" if report.reason else "")
            )
            for issue in report.issues:
                console.print(f"[yellow]issue:[/yellow] {issue}")
        ok = report.status == "completed" or (report.status == "active" and not report.issues)
        return 0 if ok else 1
    except BearError as exc:
        console.print(f"[red]{type(exc).__name__}:[/red] {container.redactor.redact(str(exc))}")
        return 1
    finally:
        unsubscribe()
        await container.close()


@tasks_app.command("plan")
def tasks_plan(
    goal: str = typer.Argument(..., help="Complex goal to decompose."),
    config: Path | None = typer.Option(None, "--config"),
    cwd: Path | None = typer.Option(None, "--cwd", "-C"),
    session: str | None = typer.Option(None, "--session", "-s", help="Existing session id."),
    run: bool = typer.Option(False, "--run", help="Execute the plan immediately."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve ASK tools while running."),
    json_output: bool = typer.Option(False, "--json", help="Emit NDJSON events while running."),
) -> None:
    """用模型生成结构化 Plan, 经程序校验后持久化."""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    workspace = _resolve_cwd(cwd)

    async def _plan() -> None:
        if not settings.tasks.enabled:
            _fail("task engine is disabled in configuration ([tasks].enabled=false)", code=2)
        try:
            settings.validate_ready()
        except ValueError as exc:
            _fail(str(exc), code=2)
        try:
            container = build_container(settings, cwd=workspace)
        except Exception as exc:
            _fail(f"failed to initialize Bear-Agent: {exc}", code=1)
            return
        try:
            if session:
                record = await container.sessions.get(session)
                if record is None:
                    _fail(f"session not found: {session}", code=2)
            else:
                record = await container.sessions.create(
                    cwd=workspace,
                    model=settings.agent.default_model,
                    provider=settings.agent.default_provider,
                    title=" ".join(goal.split())[:80] or "Task plan",
                    publish=container.bus.publish,
                )
            if await container.tasks.get_plan(record.id) is not None:
                _fail("session already has a plan; use tasks show or revise it", code=2)
            owner = f"cli-plan-{os.getpid()}"
            epoch = await container.sessions.acquire_lease(record.id, owner, 900.0)
            fence = LeaseFence(owner, epoch)
            token = set_fence(fence)
            try:
                draft = await container.planner.generate(goal, context=f"Workspace: {workspace}")
                plan = await container.tasks.create_plan(
                    record.id, draft, source="model", fence=fence
                )
                if record.title is None:
                    await container.sessions.set_title(
                        record.id, " ".join(goal.split())[:80] or "Task plan", fence=fence
                    )
            finally:
                reset_fence(token)
                with contextlib.suppress(Exception):
                    await container.sessions.release_lease(record.id, owner, epoch)
            if json_output:
                print(
                    json.dumps(
                        {
                            "type": "plan_created",
                            "session_id": record.id,
                            "plan_id": plan.id,
                            "version": plan.version,
                            "tasks": [task.key for task in draft.tasks],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            else:
                console.print(
                    f"plan [cyan]{plan.id}[/cyan] created with {len(draft.tasks)} task(s)"
                )
                await _print_plan(container.tasks, record.id)
            if run:
                await container.close()
                raise typer.Exit(
                    code=await _execute_plan(
                        settings,
                        workspace,
                        record.id,
                        json_output=json_output,
                        yes=yes,
                    )
                )
        finally:
            with contextlib.suppress(Exception):
                await container.close()

    try:
        asyncio.run(_plan())
    except typer.Exit:
        raise
    except BearError as exc:
        _fail(str(exc), code=1)
    except Exception as exc:
        _fail(f"plan generation failed: {exc}", code=1)


@tasks_app.command("show")
def tasks_show(
    session_id: str = typer.Argument(..., help="Session id."),
    config: Path | None = typer.Option(None, "--config"),
    attempts: bool = typer.Option(True, "--attempts/--no-attempts"),
) -> None:
    """查看当前 Plan、Todo 与执行历史。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _show() -> None:
        maintenance = build_maintenance(settings, cwd=Path.cwd())
        try:
            record = await maintenance.sessions.get(session_id)
            if record is None:
                _fail(f"session not found: {session_id}", code=2)
            await _print_plan(maintenance.tasks, session_id, attempts=attempts)
        finally:
            await maintenance.close()

    try:
        asyncio.run(_show())
    except typer.Exit:
        raise
    except BearError as exc:
        _fail(str(exc), code=1)
    except Exception as exc:
        _fail(f"cannot show plan: {exc}", code=1)


@tasks_app.command("run")
def tasks_run(
    session_id: str = typer.Argument(..., help="Session id."),
    config: Path | None = typer.Option(None, "--config"),
    cwd: Path | None = typer.Option(None, "--cwd", "-C"),
    json_output: bool = typer.Option(False, "--json", help="Emit NDJSON events."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve ASK tools."),
) -> None:
    """执行当前 Plan, 直到完成、阻塞或预算耗尽."""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    workspace = _resolve_cwd(cwd)
    code = asyncio.run(_execute_plan(settings, workspace, session_id, json_output=json_output))
    raise typer.Exit(code=code)


@tasks_app.command("task")
def tasks_task(
    task_id: str = typer.Argument(..., help="Task id."),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """查看单个任务的详情、依赖与尝试历史。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _task() -> None:
        maintenance = build_maintenance(settings, cwd=Path.cwd())
        try:
            task = await maintenance.tasks.get_task(task_id)
            if task is None:
                _fail(f"task not found: {task_id}", code=2)
            deps = await maintenance.tasks.dependencies(task_id)
            console.print(
                Panel(
                    f"id           [dim]{task.id}[/dim]\n"
                    f"key          {task.task_key}\n"
                    f"title        {task.title}\n"
                    f"status       {task.status.value}\n"
                    f"priority     {task.priority}\n"
                    f"attempts     {task.attempt_count}/{task.max_attempts}\n"
                    f"dependencies {', '.join(deps) or '(none)'}\n"
                    f"description  {task.description}\n"
                    f"acceptance   {task.acceptance_criteria or '(none)'}\n"
                    f"result       {task.result_summary or '(none)'}\n"
                    f"last error   {task.last_error or '(none)'}",
                    title="Task",
                )
            )
            history = await maintenance.tasks.attempts(task_id)
            if history:
                table = Table(title="Attempts")
                table.add_column("#", width=3)
                table.add_column("status")
                table.add_column("started")
                table.add_column("finished")
                table.add_column("summary / error")
                for attempt in history:
                    table.add_row(
                        str(attempt.attempt_no),
                        attempt.status.value,
                        attempt.started_at.strftime("%m-%d %H:%M"),
                        attempt.finished_at.strftime("%m-%d %H:%M") if attempt.finished_at else "-",
                        attempt.completion_summary or attempt.error_message or "",
                    )
                console.print(table)
        finally:
            await maintenance.close()

    try:
        asyncio.run(_task())
    except typer.Exit:
        raise
    except BearError as exc:
        _fail(str(exc), code=1)
    except Exception as exc:
        _fail(f"cannot show task: {exc}", code=1)


@tasks_app.command("revise")
def tasks_revise(
    session_id: str = typer.Argument(..., help="Session id."),
    plan_file: Path = typer.Option(..., "--file", help="JSON PlanDraft file."),
    reason: str = typer.Option(..., "--reason", help="Why the plan changed."),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """用用户提供的 JSON 计划修订当前 Plan (程序校验依赖与循环)."""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    try:
        data = json.loads(plan_file.read_text(encoding="utf-8"))
        draft = PlanDraft.model_validate(data)
    except Exception as exc:
        _fail(f"invalid plan file: {exc}", code=2)

    async def _revise() -> None:
        maintenance = build_maintenance(settings, cwd=Path.cwd())
        try:
            record = await maintenance.sessions.get(session_id)
            if record is None:
                _fail(f"session not found: {session_id}", code=2)
            owner = f"cli-task-{os.getpid()}"
            epoch = await maintenance.sessions.acquire_lease(session_id, owner, 60.0)
            fence = LeaseFence(owner, epoch)
            token = set_fence(fence)
            try:
                plan = await maintenance.tasks.revise_plan(
                    session_id, draft, reason=reason, source="user", fence=fence
                )
            finally:
                reset_fence(token)
                with contextlib.suppress(Exception):
                    await maintenance.sessions.release_lease(session_id, owner, epoch)
            console.print(f"plan revised to v{plan.version} with {len(draft.tasks)} task(s)")
            await _print_plan(maintenance.tasks, session_id)
        finally:
            await maintenance.close()

    try:
        asyncio.run(_revise())
    except typer.Exit:
        raise
    except BearError as exc:
        _fail(str(exc), code=1)
    except Exception as exc:
        _fail(f"revision failed: {exc}", code=1)


@tasks_app.command("cancel")
def tasks_cancel(
    session_id: str = typer.Argument(..., help="Session id."),
    config: Path | None = typer.Option(None, "--config"),
    task_id: str | None = typer.Option(None, "--task", help="Cancel one task instead of the plan."),
    reason: str = typer.Option("cancelled by user", "--reason"),
) -> None:
    """取消当前 Plan 或某个任务。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _cancel() -> None:
        maintenance = build_maintenance(settings, cwd=Path.cwd())
        try:
            record = await maintenance.sessions.get(session_id)
            if record is None:
                _fail(f"session not found: {session_id}", code=2)
            owner = f"cli-task-{os.getpid()}"
            epoch = await maintenance.sessions.acquire_lease(session_id, owner, 60.0)
            fence = LeaseFence(owner, epoch)
            token = set_fence(fence)
            try:
                if task_id:
                    task = await maintenance.tasks.cancel_task(task_id, reason=reason, fence=fence)
                    console.print(f"task {task.task_key} cancelled")
                else:
                    plan = await maintenance.tasks.cancel_plan(
                        session_id, reason=reason, fence=fence
                    )
                    if plan is None:
                        _fail("session has no plan", code=2)
                    console.print("plan cancelled")
            finally:
                reset_fence(token)
                with contextlib.suppress(Exception):
                    await maintenance.sessions.release_lease(session_id, owner, epoch)
        finally:
            await maintenance.close()

    try:
        asyncio.run(_cancel())
    except typer.Exit:
        raise
    except BearError as exc:
        _fail(str(exc), code=1)
    except Exception as exc:
        _fail(f"cancel failed: {exc}", code=1)


@tasks_app.command("retry")
def tasks_retry(
    task_id: str = typer.Argument(..., help="Task id."),
    config: Path | None = typer.Option(None, "--config"),
    force: bool = typer.Option(
        False, "--force", help="Allow one extra attempt after the max attempt count."
    ),
) -> None:
    """重试 failed/blocked/cancelled 任务 (受尝试次数上限约束)."""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _retry() -> None:
        maintenance = build_maintenance(settings, cwd=Path.cwd())
        try:
            task = await maintenance.tasks.get_task(task_id)
            if task is None:
                _fail(f"task not found: {task_id}", code=2)
            owner = f"cli-task-{os.getpid()}"
            epoch = await maintenance.sessions.acquire_lease(task.session_id, owner, 60.0)
            fence = LeaseFence(owner, epoch)
            token = set_fence(fence)
            try:
                updated = await maintenance.tasks.retry_task(task_id, fence=fence, force=force)
            finally:
                reset_fence(token)
                with contextlib.suppress(Exception):
                    await maintenance.sessions.release_lease(task.session_id, owner, epoch)
            console.print(f"task {updated.task_key} is {updated.status.value}")
        finally:
            await maintenance.close()

    try:
        asyncio.run(_retry())
    except typer.Exit:
        raise
    except BearError as exc:
        _fail(str(exc), code=1)
    except Exception as exc:
        _fail(f"retry failed: {exc}", code=1)


@tasks_app.command("resume")
def tasks_resume(
    session_id: str = typer.Argument(..., help="Session id."),
    config: Path | None = typer.Option(None, "--config"),
    cwd: Path | None = typer.Option(None, "--cwd", "-C"),
    run: bool = typer.Option(False, "--run", help="Run the resumed plan."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Retry tasks whose last attempt used non-idempotent tools.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve ASK tools while running."),
) -> None:
    """恢复崩溃后的任务状态并重试可重试任务。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    workspace = _resolve_cwd(cwd)

    async def _resume() -> list[str]:
        maintenance = build_maintenance(settings, cwd=Path.cwd())
        try:
            record = await maintenance.sessions.get(session_id)
            if record is None:
                _fail(f"session not found: {session_id}", code=2)
            owner = f"cli-task-{os.getpid()}"
            epoch = await maintenance.sessions.acquire_lease(session_id, owner, 120.0)
            fence = LeaseFence(owner, epoch)
            token = set_fence(fence)
            retried: list[str] = []
            try:
                await maintenance.tasks.reconcile_after_crash(session_id, fence=fence)
                for task in await maintenance.tasks.list_tasks(session_id):
                    if task.status in {
                        TaskStatus.BLOCKED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                    }:
                        try:
                            updated = await maintenance.tasks.retry_task(
                                task.id, fence=fence, force=force
                            )
                            retried.append(updated.task_key)
                        except TaskStateError as exc:
                            retried.append(f"{task.task_key}: {exc}")
            finally:
                reset_fence(token)
                with contextlib.suppress(Exception):
                    await maintenance.sessions.release_lease(session_id, owner, epoch)
            return retried
        finally:
            await maintenance.close()

    try:
        retried = asyncio.run(_resume())
    except typer.Exit:
        raise
    except BearError as exc:
        _fail(str(exc), code=1)
        raise AssertionError("unreachable") from exc
    except Exception as exc:
        _fail(f"resume failed: {exc}", code=1)
        raise AssertionError("unreachable") from exc
    if retried:
        console.print("resumed: " + ", ".join(retried))
    else:
        console.print("no runnable tasks to resume")
    if run:
        raise typer.Exit(
            code=asyncio.run(_execute_plan(settings, workspace, session_id, json_output=False))
        )


# ---------------------------------------------------------------------- config


@config_app.command("show")
def config_show(
    config: Path | None = typer.Option(None, "--config"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """输出当前生效配置(密钥已脱敏)。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    if as_json:
        data = settings.model_dump(mode="json")
        for provider in data.get("providers", {}).values():
            provider.pop("api_key", None)
            provider.pop("headers", None)
        # shell.env and MCP env/headers may contain user-supplied secrets;
        # never print them.
        data.get("shell", {}).pop("env", None)
        for server in (data.get("mcp", {}).get("servers") or {}).values():
            server.pop("env", None)
            server.pop("headers", None)
        console.print_json(json.dumps(data, ensure_ascii=False))
    else:
        console.print(settings_to_toml(settings), markup=False)


@config_app.command("path")
def config_path() -> None:
    """输出默认配置路径。"""
    console.print(str(default_config_path()))


# ------------------------------------------------------------------- providers


def _provider_settings(
    config: Path | None,
    *,
    provider: str | None = None,
    model: str | None = None,
    fallbacks: list[str] | None = None,
) -> Settings:
    overrides: dict[str, Any] = {"agent": {}}
    if provider:
        overrides["agent"]["default_provider"] = provider
    if model:
        overrides["agent"]["default_model"] = model
    if fallbacks:
        overrides["agent"]["fallback_models"] = fallbacks
    try:
        return load_settings(config, overrides=overrides)
    except BearError as exc:
        _fail(str(exc), code=2)
    except Exception as exc:
        _fail(str(exc), code=2)
    raise AssertionError("unreachable")


@providers_app.command("show")
def providers_show(
    config: Path | None = typer.Option(None, "--config"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """显示 Primary、Fallback、能力与重试策略 (不输出密钥)."""
    settings = _provider_settings(config)
    try:
        provider = build_fallback_provider(
            settings,
            secret_store=secret_store_from_config(settings.secrets.backend),
        )
        snapshot = provider.snapshot()
    except BearError as exc:
        console.print(f"[yellow]provider router unavailable:[/yellow] {exc}")
        console.print(
            f"primary={settings.agent.default_provider}:{settings.agent.default_model} "
            f"fallbacks={settings.agent.fallback_models}"
        )
        return
    finally:
        with contextlib.suppress(Exception):
            if "provider" in locals():
                asyncio.run(provider.aclose())
    if as_json:
        console.print_json(json.dumps(snapshot, ensure_ascii=False))
        return
    table = Table(title="Provider routes")
    table.add_column("#", width=2)
    table.add_column("provider", style="cyan")
    table.add_column("model")
    table.add_column("context")
    table.add_column("tools")
    table.add_column("stream")
    table.add_column("retries")
    for index, route in enumerate(provider.routes, start=1):
        table.add_row(
            str(index),
            route.name,
            route.model,
            str(route.capabilities.context_window),
            "yes" if route.capabilities.supports_tools else "no",
            "yes" if route.capabilities.supports_streaming else "no",
            str(route.retry_policy.max_attempts),
        )
    console.print(table)
    console.print(
        f"fallback_enabled={settings.agent.provider_fallback_enabled} "
        f"circuit_failures={settings.agent.circuit_breaker_failures} "
        f"cooldown={settings.agent.circuit_breaker_cooldown_s:.0f}s"
    )
    for error in snapshot.get("configuration_errors") or []:
        console.print(f"[yellow]config issue:[/yellow] {error}")


@providers_app.command("test")
def providers_test(
    config: Path | None = typer.Option(None, "--config"),
    provider_name: str | None = typer.Option(None, "--provider"),
    model: str | None = typer.Option(None, "--model"),
    prompt: str = typer.Option("Reply with the single word: pong", "--prompt"),
) -> None:
    """发送一个最小请求, 验证 Primary / Fallback 是否可用."""
    settings = _provider_settings(config, provider=provider_name, model=model)
    try:
        settings.validate_ready()
    except ValueError as exc:
        _fail(str(exc), code=2)

    async def _call() -> int:
        router = build_fallback_provider(
            settings,
            secret_store=secret_store_from_config(settings.secrets.backend),
        )
        caller = ModelCaller(router)
        try:
            response = await caller.call(
                ModelRequest(
                    model=settings.agent.default_model,
                    messages=[
                        SystemMessage.of("You are a health check."),
                        UserMessage.of(prompt),
                    ],
                    tools=[],
                    temperature=0.0,
                    max_output_tokens=16,
                    stream=False,
                ),
                CancelToken(),
            )
        except Exception as exc:
            console.print(f"[red]provider test failed:[/red] {type(exc).__name__}: {exc}")
            for route_status in router.status():
                console.print(
                    f"  {route_status['provider']}/{route_status['model']}: "
                    f"{route_status['state']} failures={route_status['consecutive_failures']}"
                )
            return 1
        finally:
            await router.aclose()
        route = router.last_route
        console.print(
            f"[green]ok[/green] provider={route[0] if route else '?'} "
            f"model={route[1] if route else '?'} "
            f"reply={response.text[:80]!r}"
        )
        return 0

    raise typer.Exit(code=asyncio.run(_call()))


# --------------------------------------------------------------------- plugins


def _plugin_manager(settings: Settings) -> PluginManager:
    return PluginManager(
        settings.plugins,
        data_dir=settings.ensure_data_dirs(),
        hooks_allow_external=settings.hooks.allow_external,
        hooks_enabled=settings.hooks.enabled,
    )


@plugins_app.command("list")
def plugins_list(
    config: Path | None = typer.Option(None, "--config"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列出发现的 Plugin 与启用状态。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    manager = _plugin_manager(settings)
    infos = manager.list()
    if as_json:
        console.print_json(
            json.dumps([info.model_dump(mode="json") for info in infos], ensure_ascii=False)
        )
        return
    table = Table(title="Plugins")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("version")
    table.add_column("enabled")
    table.add_column("status")
    table.add_column("hooks")
    table.add_column("mcp")
    table.add_column("python")
    for info in infos:
        table.add_row(
            info.plugin_id,
            info.version,
            "yes" if info.enabled else "no",
            info.status.value,
            ",".join(info.hooks),
            ",".join(info.mcp_servers),
            "yes" if info.python else "no",
        )
    console.print(table)
    status = manager.status()
    for error in status.get("discovery_errors") or []:
        console.print(f"[yellow]discovery:[/yellow] {error}")
    for error in status.get("conflicts") or []:
        console.print(f"[yellow]conflict:[/yellow] {error}")


@plugins_app.command("validate")
def plugins_validate(
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """校验所有 Plugin Manifest、依赖与权限。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    manager = _plugin_manager(settings)
    report = manager.validate_all()
    for plugin in report.plugins:
        state = "ok" if plugin.ok else "invalid"
        console.print(f"[cyan]{plugin.plugin_id}[/cyan] {state} [dim]{plugin.path}[/dim]")
        for error in plugin.errors:
            console.print(f"  [red]error:[/red] {error}")
        for warning in plugin.warnings:
            console.print(f"  [yellow]warn:[/yellow] {warning}")
    for error in manager.status().get("discovery_errors") or []:
        console.print(f"[red]error:[/red] {error}")
    if not report.ok:
        raise typer.Exit(code=1)


@plugins_app.command("info")
def plugins_info(
    plugin_id: str = typer.Argument(...),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """查看一个 Plugin 的详情。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    info = _plugin_manager(settings).info(plugin_id)
    if info is None:
        _fail(f"unknown plugin: {plugin_id}", code=2)
    console.print_json(json.dumps(info.model_dump(mode="json"), ensure_ascii=False))


@plugins_app.command("enable")
def plugins_enable(
    plugin_id: str = typer.Argument(...),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """启用 Plugin (下次启动生效)."""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    try:
        _plugin_manager(settings).enable(plugin_id)
    except Exception as exc:
        _fail(str(exc), code=1)
    console.print(f"plugin {plugin_id} enabled")


@plugins_app.command("disable")
def plugins_disable(
    plugin_id: str = typer.Argument(...),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """禁用 Plugin。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    manager = _plugin_manager(settings)

    async def _disable() -> None:
        await manager.disable(plugin_id)

    try:
        asyncio.run(_disable())
    except Exception as exc:
        _fail(str(exc), code=1)
    console.print(f"plugin {plugin_id} disabled")


# ------------------------------------------------------------------------- mcp


def _mcp_manager(settings: Settings) -> tuple[MCPManager, ToolRegistry]:
    registry = ToolRegistry()
    manager = MCPManager(
        settings.mcp,
        registry,
        workspace=Path.cwd(),
        server_cwd=settings.ensure_data_dirs() / "mcp",
        redactor=SecretRedactor(),
        secret_store=secret_store_from_config(settings.secrets.backend),
    )
    return manager, registry


@mcp_app.command("status")
def mcp_status(
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """连接已配置的 MCP Server 并显示状态。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    manager, _registry = _mcp_manager(settings)

    async def _status() -> int:
        try:
            statuses = await manager.start()
        except Exception as exc:
            console.print(f"[red]mcp startup failed:[/red] {exc}")
            return 1
        finally:
            with contextlib.suppress(Exception):
                await manager.stop()
        table = Table(title="MCP servers")
        table.add_column("server", style="cyan")
        table.add_column("transport")
        table.add_column("state")
        table.add_column("required")
        table.add_column("tools")
        table.add_column("last error")
        for status in statuses.values():
            table.add_row(
                status.name,
                status.transport,
                status.state,
                "yes" if status.required else "no",
                str(len(status.tools)),
                status.last_error or "",
            )
        console.print(table)
        return 0

    raise typer.Exit(code=asyncio.run(_status()))


@mcp_app.command("tools")
def mcp_tools(
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """列出 MCP 发现并注册的工具。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    manager, registry = _mcp_manager(settings)

    async def _tools() -> int:
        rows: list[tuple[str, str]] = []
        try:
            await manager.start()
            for name in registry.names():
                tool = registry.get(name)
                if tool is not None:
                    rows.append((name, tool.description))
        except Exception as exc:
            console.print(f"[red]mcp startup failed:[/red] {exc}")
            return 1
        finally:
            with contextlib.suppress(Exception):
                await manager.stop()
        for name, description in rows:
            console.print(f"[cyan]{name}[/cyan] {description}")
        return 0

    raise typer.Exit(code=asyncio.run(_tools()))


# ----------------------------------------------------------------------- hooks


@hooks_app.command("list")
def hooks_list(
    config: Path | None = typer.Option(None, "--config"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列出已配置与已注册的 Hook。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    manager = HookManager(
        config=settings.hooks,
        workspace=Path.cwd(),
        safe_cwd=settings.ensure_data_dirs() / "hooks",
    )
    rows = manager.list()
    if as_json:
        console.print_json(json.dumps(rows, ensure_ascii=False))
        return
    table = Table(title="Hooks")
    table.add_column("id", style="cyan")
    table.add_column("events")
    table.add_column("source")
    table.add_column("fail")
    table.add_column("deny")
    table.add_column("context")
    for row in rows:
        table.add_row(
            str(row.get("id")),
            ",".join(row.get("events") or []),
            str(row.get("source")),
            str(row.get("fail_mode")),
            "yes" if row.get("can_deny") else "no",
            "yes" if row.get("can_append_context") else "no",
        )
    console.print(table)


# ------------------------------------------------------------------ trace/metrics/bench


def _settings_only(config: Path | None) -> Settings:
    return _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )


def _parse_since(value: str | None, *, default_days: int = 7) -> datetime | None:
    if value is None:
        return datetime.now(UTC) - timedelta(days=default_days)
    text = value.strip().lower()
    if not text or text == "all":
        return None
    units = {"s": 1, "m": 60, "h": 3_600, "d": 86_400, "w": 604_800}
    if text[-1:] in units and text[:-1].isdigit():
        return datetime.now(UTC) - timedelta(seconds=int(text[:-1]) * units[text[-1]])
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _fail(f"invalid time window {value!r}; use 30m, 24h, 7d, 4w or ISO-8601", code=2)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _trace_service(settings: Settings, db: Database) -> TraceService:
    return TraceService(
        db,
        config=settings.observability,
        redactor=SecretRedactor(),
    )


@trace_app.command("list")
def trace_list(
    config: Path | None = typer.Option(None, "--config"),
    session: str | None = typer.Option(None, "--session", help="Filter by session id."),
    run: str | None = typer.Option(None, "--run", help="Filter by run id."),
    status: str | None = typer.Option(
        None, "--status", help="running|ok|error|cancelled|incomplete"
    ),
    since: str = typer.Option("7d", "--since", help="Time window (24h, 7d, ISO-8601, all)."),
    limit: int = typer.Option(20, "--limit", min=1, max=500),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列出最近的 Trace。"""
    settings = _settings_only(config)
    since_dt = _parse_since(since)

    async def _list() -> list[dict[str, Any]]:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            return await _trace_service(settings, db).list_traces(
                session_id=session,
                run_id=run,
                status=status,
                since=since_dt,
                limit=limit,
            )
        finally:
            db.close()

    rows = asyncio.run(_list())
    if as_json:
        console.print_json(json.dumps(rows, ensure_ascii=False, default=str))
        return
    if not rows:
        console.print("[dim]no traces in this window[/dim]")
        return
    table = Table(title="Traces")
    table.add_column("trace", style="cyan", no_wrap=True)
    table.add_column("started", style="dim")
    table.add_column("kind")
    table.add_column("status")
    table.add_column("duration")
    table.add_column("spans", justify="right")
    table.add_column("session")
    table.add_column("run")
    for row in rows:
        color = _status_color(str(row["status"]))
        table.add_row(
            _short(row["trace_id"]),
            str(row["started_at"])[:19],
            str(row["kind"]),
            f"[{color}]{row['status']}[/{color}]",
            f"{row['duration_ms'] or 0} ms",
            str(row["span_count"]),
            _short(row["session_id"]),
            _short(row["run_id"] or ""),
        )
    console.print(table)


@trace_app.command("show")
def trace_show(
    trace_id: str = typer.Argument(..., help="Trace id."),
    config: Path | None = typer.Option(None, "--config"),
    as_json: bool = typer.Option(False, "--json"),
    events: bool = typer.Option(False, "--events", help="Also show linked event types."),
) -> None:
    """显示一次执行的 Span 树与关键路径。"""
    settings = _settings_only(config)

    async def _show() -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            service = _trace_service(settings, db)
            detail = await service.hierarchy(trace_id)
            linked = await service.events(trace_id) if events else []
            return detail, linked
        finally:
            db.close()

    detail, linked = asyncio.run(_show())
    if detail is None:
        _fail(f"trace not found: {trace_id}", code=2)
    if as_json:
        console.print_json(json.dumps({"detail": detail, "events": linked}, ensure_ascii=False))
        return
    trace = detail["trace"]
    color = _status_color(str(trace["status"]))
    tree = Tree(
        f"[bold]{trace['name']}[/bold] [dim]{trace['trace_id']}[/dim] "
        f"[{color}]{trace['status']}[/{color}] {trace['duration_ms']} ms"
    )
    for root in detail["roots"]:
        _render_span(root, tree)
    console.print(tree)
    if trace.get("error_message"):
        console.print(f"[red]{trace['error_message']}[/red]")
    if linked:
        table = Table(title="Linked events")
        table.add_column("seq", justify="right")
        table.add_column("type")
        table.add_column("span", style="dim")
        for event in linked:
            table.add_row(str(event["seq"]), str(event["type"]), _short(event.get("span_id") or ""))
        console.print(table)


@trace_app.command("run")
def trace_run(
    run_id: str = typer.Argument(..., help="Run id."),
    config: Path | None = typer.Option(None, "--config"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """按 Run ID 还原执行路径。"""
    settings = _settings_only(config)

    async def _find() -> list[dict[str, Any]]:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            return await _trace_service(settings, db).find_by_run(run_id)
        finally:
            db.close()

    rows = asyncio.run(_find())
    if not rows:
        _fail(f"no trace for run: {run_id}", code=2)
    if as_json:
        console.print_json(json.dumps(rows, ensure_ascii=False, default=str))
        return
    trace_show(rows[0]["trace_id"], config=config, as_json=False, events=False)


@trace_app.command("export")
def trace_export(
    output: Path = typer.Option(..., "--output", "-o", help="Destination JSON file."),
    config: Path | None = typer.Option(None, "--config"),
    trace_id: str | None = typer.Option(None, "--trace", help="Explicit trace id."),
    run: str | None = typer.Option(None, "--run", help="Latest trace for a run id."),
    session: str | None = typer.Option(None, "--session", help="Latest trace for a session id."),
) -> None:
    """导出一个 Trace (含脱敏事件), 用于离线诊断。"""
    settings = _settings_only(config)

    async def _export() -> dict[str, Any] | None:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            service = _trace_service(settings, db)
            selected = trace_id
            if selected is None and run is not None:
                rows = await service.find_by_run(run)
                selected = rows[0]["trace_id"] if rows else None
            if selected is None and session is not None:
                rows = await service.find_by_session(session, limit=1)
                selected = rows[0]["trace_id"] if rows else None
            if selected is None:
                return None
            return await service.export(selected)
        finally:
            db.close()

    document = asyncio.run(_export())
    if document is None:
        _fail("no trace selected; pass --trace, --run or --session", code=2)
    write_json(output, document)
    console.print(f"trace exported to [cyan]{output}[/cyan]")


@trace_app.command("stats")
def trace_stats(
    config: Path | None = typer.Option(None, "--config"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """显示 Trace 存储计数与不完整 Trace。"""
    settings = _settings_only(config)

    async def _stats() -> dict[str, Any]:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            return await _trace_service(settings, db).stats()
        finally:
            db.close()

    stats = asyncio.run(_stats())
    if as_json:
        console.print_json(json.dumps(stats, ensure_ascii=False))
        return
    for key, value in stats.items():
        console.print(f"{key:12s} {value}")


@trace_app.command("prune")
def trace_prune(
    config: Path | None = typer.Option(None, "--config"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm deletion."),
) -> None:
    """按保留策略删除旧 Trace。"""
    if not yes:
        _fail("refusing to delete traces without --yes", code=2)
    settings = _settings_only(config)

    async def _prune() -> int:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            return await _trace_service(settings, db).prune()
        finally:
            db.close()

    console.print(f"deleted {asyncio.run(_prune())} trace(s)")


def _render_span(span_node: dict[str, Any], tree: Tree) -> None:
    color = _status_color(str(span_node["status"]))
    attrs = span_node.get("attributes") or {}
    interesting = [
        f"{key}={attrs[key]}"
        for key in (
            "provider",
            "model",
            "tool",
            "tool_call_id",
            "hook_id",
            "level",
            "outcome",
            "task_key",
            "checkpoint_id",
            "server",
            "error_kind",
        )
        if key in attrs
    ]
    label = (
        f"[{color}]{span_node['status']}[/{color}] "
        f"[bold]{span_node['kind']}[/bold] {span_node['name']} "
        f"[dim]{span_node['duration_ms']} ms[/dim]"
    )
    if interesting:
        label += " [dim]" + " ".join(interesting) + "[/dim]"
    branch = tree.add(label)
    for child in span_node.get("children") or []:
        _render_span(child, branch)


def _status_color(status: str) -> str:
    return {
        "ok": "green",
        "completed": "green",
        "error": "red",
        "failed": "red",
        "cancelled": "yellow",
        "incomplete": "yellow",
        "running": "cyan",
    }.get(status, "white")


def _short(value: str, length: int = 14) -> str:
    return value if len(value) <= length else value[: length - 1] + "…"


# ---------------------------------------------------------------------- metrics


@metrics_app.command("show")
def metrics_show(
    config: Path | None = typer.Option(None, "--config"),
    since: str = typer.Option("7d", "--since"),
    session: str | None = typer.Option(None, "--session"),
    section: str = typer.Option(
        "all", "--section", help="all|runs|tasks|models|tools|context|recovery|system"
    ),
    as_json: bool = typer.Option(False, "--json"),
    export: Path | None = typer.Option(None, "--export"),
) -> None:
    """聚合 Runtime、Task、Provider、Tool、Context 与 Recovery 指标。"""
    settings = _settings_only(config)
    since_dt = _parse_since(since)
    report = _metrics_report(settings, since_dt, session)
    if export is not None:
        write_json(export, report)
        if not as_json:
            console.print(f"metrics exported to [cyan]{export}[/cyan]")
    if as_json:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return
    known_sections = {"runs", "tasks", "models", "tools", "context", "recovery", "system"}
    if section != "all" and section not in known_sections:
        _fail(
            f"unknown metrics section {section!r}; choose all or {sorted(known_sections)}", code=2
        )
    sections = (
        ["runs", "tasks", "models", "tools", "context", "recovery", "system"]
        if section == "all"
        else [section]
    )
    for name in sections:
        _render_metric_section(name, report)
    for note in report.get("notes") or []:
        console.print(f"[dim]note: {note}[/dim]")


def _metrics_report(
    settings: Settings, since: datetime | None, session: str | None
) -> dict[str, Any]:
    if not settings.observability.enabled or not settings.observability.metrics_enabled:
        _fail("metrics are disabled by [observability] configuration", code=2)

    async def _report() -> dict[str, Any]:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            return await MetricsService(
                db, config=settings.observability, settings=settings
            ).report(since=since, session_id=session)
        finally:
            db.close()

    return asyncio.run(_report())


def _render_metric_section(name: str, report: dict[str, Any]) -> None:
    if name == "runs":
        runs = report["runs"]
        table = Table(title="Runs")
        for key in ("total", "success_rate", "completed", "failed", "cancelled"):
            table.add_column(key)
        table.add_row(
            str(runs["total"]),
            f"{runs['success_rate']:.2%}",
            str(runs["completed"]),
            str(runs["failed"]),
            str(runs["cancelled"]),
        )
        console.print(table)
        _render_distribution("Run latency", runs["duration_ms"], suffix="ms")
    elif name == "tasks":
        tasks = report["tasks"]
        table = Table(title="Tasks")
        for key in ("total", "success_rate", "completed", "failed", "blocked", "retries"):
            table.add_column(key)
        table.add_row(
            str(tasks["total"]),
            f"{tasks['success_rate']:.2%}",
            str(tasks["completed"]),
            str(tasks["failed"]),
            str(tasks["blocked"]),
            str(tasks["retries"]),
        )
        console.print(table)
        console.print(
            f"[dim]plan revisions: {tasks['plan_revisions']}; attempts: {tasks['attempts']}[/dim]"
        )
        _render_distribution("Task latency", tasks["duration_ms"], suffix="ms")
    elif name == "models":
        models = report["models"]
        console.print(
            f"[bold]Model calls[/bold] calls={models['calls']} "
            f"success={models['success_rate']:.2%} retries={models['retries']} "
            f"fallbacks={models['fallbacks']} circuits={models['circuit_opens']}"
        )
        console.print(
            f"[dim]tokens in={models['input_tokens']} out={models['output_tokens']} "
            f"cache_read={models['cache_read_tokens']} "
            f"usage_missing={models['usage_missing']} "
            f"pricing_missing={models['pricing_missing']} "
            f"cost={models['cost_by_currency']}[/dim]"
        )
        _render_distribution("Model latency", models["latency_ms"], suffix="ms")
        if models["by_provider"]:
            table = Table(title="Provider / model")
            for key in ("provider", "model", "calls", "failures", "tokens", "cost"):
                table.add_column(key)
            for item in models["by_provider"]:
                table.add_row(
                    item["provider"],
                    item["model"],
                    str(item["calls"]),
                    str(item["failures"]),
                    str(item["input_tokens"] + item["output_tokens"]),
                    f"{item['cost']} {item['currency'] or ''}".strip(),
                )
            console.print(table)
    elif name == "tools":
        tools = report["tools"]
        console.print(
            f"[bold]Tool calls[/bold] calls={tools['calls']} "
            f"success={tools['success_rate']:.2%} timeouts={tools['timeouts']} "
            f"cancelled={tools['cancelled']} unknown={tools['unknown']}"
        )
        console.print(
            f"[dim]permission={tools['permission']} approvals={tools['approvals']} "
            f"sandbox={tools['sandbox']['runs']} "
            f"mcp={tools['mcp']['calls']} hooks={tools['hooks']['status_counts']}[/dim]"
        )
        _render_distribution("Tool latency", tools["duration_ms"], suffix="ms")
        if tools["by_tool"]:
            table = Table(title="Tool breakdown")
            for key in ("tool", "calls", "success", "mean_ms", "p95"):
                table.add_column(key)
            for item in tools["by_tool"][:20]:
                table.add_row(
                    item["name"],
                    str(item["calls"]),
                    f"{item['success_rate']:.2%}",
                    str(item["mean_ms"]),
                    str(item["duration_ms"]["p95"]),
                )
            console.print(table)
    elif name == "context":
        context = report["context"]
        table = Table(title="Context")
        for key in ("builds", "compactions", "saved_tokens", "artifacts", "failures", "levels"):
            table.add_column(key)
        table.add_row(
            str(context["builds"]),
            str(context["compactions"]),
            str(context["saved_tokens"]),
            str(context["artifacts"]),
            str(context["failures"]),
            str(context["levels"]),
        )
        console.print(table)
        console.print(
            f"[dim]anchors active={context['anchors']['active']} "
            f"added={context['anchors']['added']} removed={context['anchors']['removed']}[/dim]"
        )
    elif name == "recovery":
        recovery = report["recovery"]
        interesting = {
            key: value for key, value in recovery.items() if isinstance(value, (int, float, str))
        }
        table = Table(title="Recovery / checkpoint")
        table.add_column("metric")
        table.add_column("value")
        for key, value in interesting.items():
            table.add_row(key, str(value))
        console.print(table)
    elif name == "system":
        system = report["system"]
        table = Table(title="System")
        table.add_column("metric")
        table.add_column("value")
        for key in (
            "platform",
            "python",
            "database_bytes",
            "wal_bytes",
            "process_memory_bytes",
            "cpu_time_s",
            "sessions",
            "running_tools",
            "active_claims",
            "traces",
            "benchmarks",
        ):
            table.add_row(key, str(system.get(key)))
        console.print(table)


def _render_distribution(title: str, distribution: dict[str, Any], *, suffix: str) -> None:
    if not distribution.get("count"):
        console.print(f"[dim]{title}: no samples[/dim]")
        return
    console.print(
        f"[bold]{title}[/bold] n={distribution['count']} "
        f"mean={distribution['mean']}{suffix} "
        f"p50={distribution['p50']}{suffix} "
        f"p95={distribution['p95']}{suffix} "
        f"p99={distribution['p99']}{suffix} "
        f"max={distribution['max']}{suffix}"
    )


@metrics_app.command("providers")
def metrics_providers(
    config: Path | None = typer.Option(None, "--config"),
    since: str = typer.Option("7d", "--since"),
    session: str | None = typer.Option(None, "--session"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Provider 统计。"""
    report = _metrics_report(_settings_only(config), _parse_since(since), session)
    if as_json:
        console.print_json(json.dumps(report["models"], ensure_ascii=False))
    else:
        _render_metric_section("models", report)


@metrics_app.command("tools")
def metrics_tools(
    config: Path | None = typer.Option(None, "--config"),
    since: str = typer.Option("7d", "--since"),
    session: str | None = typer.Option(None, "--session"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Tool 统计。"""
    report = _metrics_report(_settings_only(config), _parse_since(since), session)
    if as_json:
        console.print_json(json.dumps(report["tools"], ensure_ascii=False))
    else:
        _render_metric_section("tools", report)


@metrics_app.command("tasks")
def metrics_tasks(
    config: Path | None = typer.Option(None, "--config"),
    since: str = typer.Option("7d", "--since"),
    session: str | None = typer.Option(None, "--session"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Task 统计。"""
    report = _metrics_report(_settings_only(config), _parse_since(since), session)
    if as_json:
        console.print_json(json.dumps(report["tasks"], ensure_ascii=False))
    else:
        _render_metric_section("tasks", report)


@metrics_app.command("recovery")
def metrics_recovery(
    config: Path | None = typer.Option(None, "--config"),
    since: str = typer.Option("7d", "--since"),
    session: str | None = typer.Option(None, "--session"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Recovery 与 Checkpoint 统计。"""
    report = _metrics_report(_settings_only(config), _parse_since(since), session)
    if as_json:
        console.print_json(json.dumps(report["recovery"], ensure_ascii=False))
    else:
        _render_metric_section("recovery", report)


@metrics_app.command("export")
def metrics_export(
    output: Path = typer.Option(..., "--output", "-o"),
    config: Path | None = typer.Option(None, "--config"),
    since: str = typer.Option("7d", "--since"),
    session: str | None = typer.Option(None, "--session"),
) -> None:
    """导出完整 Metrics 报告为 JSON。"""
    report = _metrics_report(_settings_only(config), _parse_since(since), session)
    write_json(output, report)
    console.print(f"metrics exported to [cyan]{output}[/cyan]")


# -------------------------------------------------------------------- benchmark


@bench_app.command("list")
def bench_list(
    category: str | None = typer.Option(None, "--category"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列出内置 Benchmark 场景。"""
    registry = build_registry()
    scenarios = registry.list(category=category)
    if as_json:
        console.print_json(json.dumps([item.to_dict() for item in scenarios], ensure_ascii=False))
        return
    table = Table(title="Benchmark scenarios")
    table.add_column("id", style="cyan")
    table.add_column("category")
    table.add_column("kind")
    table.add_column("network")
    table.add_column("description")
    for item in scenarios:
        table.add_row(
            item.id,
            item.category,
            item.kind,
            "yes" if item.requires_network else "no",
            item.description,
        )
    console.print(table)


@bench_app.command("run")
def bench_run(
    scenario: str | None = typer.Argument(None, help="Scenario id; omit with --all or --category."),
    config: Path | None = typer.Option(None, "--config"),
    cwd: Path | None = typer.Option(None, "--cwd", "-C"),
    all_scenarios: bool = typer.Option(False, "--all", help="Run every scenario."),
    category: str | None = typer.Option(
        None, "--category", help="Run every scenario in a category."
    ),
    iterations: int | None = typer.Option(None, "--iterations", min=1),
    warmup: int | None = typer.Option(None, "--warmup", min=0),
    param: list[str] = typer.Option(
        [], "--param", help="Scenario parameter key=value (repeatable)."
    ),
    trace: bool = typer.Option(False, "--trace", help="Trace each iteration (adds overhead)."),
    save: bool = typer.Option(
        True, "--save/--no-save", help="Persist results for history/compare."
    ),
    allow_network: bool = typer.Option(False, "--allow-network", help="Allow real provider calls."),
    as_json: bool = typer.Option(False, "--json"),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    """运行一个或多个 Benchmark 并保存结构化结果。"""
    settings = _settings_only(config)
    registry = build_registry()
    if all_scenarios or category:
        selected = [item.id for item in registry.list(category=category)]
        if iterations is None:
            iterations = 1
    elif scenario:
        selected = [scenario]
    else:
        _fail("specify a scenario, --all or --category", code=2)
    if not selected:
        _fail("the selected category contains no benchmark scenarios", code=2)
    try:
        for scenario_id in selected:
            registry.get(scenario_id)
    except KeyError as exc:
        _fail(str(exc), code=2)
    try:
        params = parse_params(param)
    except ValueError as exc:
        _fail(str(exc), code=2)
    workspace = _resolve_cwd(cwd)
    data_dir = settings.ensure_data_dirs() if save else settings.resolved_data_dir()
    db = Database(data_dir / "bear.db") if save else None
    results: list[dict[str, Any]] = []
    try:
        runner = BenchmarkRunner(settings, registry=registry, cwd=workspace, db=db)
        for scenario_id in selected:
            try:
                result = asyncio.run(
                    runner.run(
                        scenario_id,
                        iterations=iterations,
                        warmup=warmup,
                        params=params,
                        trace=trace,
                        save=save,
                        allow_network=allow_network,
                    )
                )
            except BenchmarkSkipped as exc:
                if as_json:
                    results.append(
                        {
                            "scenario": {"id": scenario_id},
                            "status": "skipped",
                            "skip_reason": str(exc),
                            "samples": [],
                            "stats": {},
                        }
                    )
                else:
                    console.print(f"[yellow]{scenario_id}: skipped: {exc}[/yellow]")
                continue
            results.append(result)
            if not as_json:
                _print_benchmark_result(result)
    finally:
        if db is not None:
            db.close()
    if output is not None:
        write_json(output, {"schema_version": 1, "results": results})
        if not as_json:
            console.print(f"benchmark results written to [cyan]{output}[/cyan]")
    if as_json:
        console.print_json(json.dumps(results, ensure_ascii=False, default=str))
    if any(result["status"] == "failed" for result in results):
        raise typer.Exit(code=1)


def _print_benchmark_result(result: dict[str, Any]) -> None:
    stats = result["stats"]
    duration = stats["duration_ms"]
    color = _status_color("ok" if result["status"] == "completed" else result["status"])
    console.print(
        f"[{color}]{result['scenario']['id']}[/{color}] {result['status']} "
        f"n={stats['successful']}/{stats['iterations']} "
        f"mean={duration['mean']}ms p50={duration['p50']}ms "
        f"p95={duration['p95']}ms p99={duration['p99']}ms "
        f"throughput={stats['throughput_per_s']}/s "
        f"errors={stats['error_rate']:.2%} tokens={stats['tokens']} "
        f"cost={stats['cost']}"
    )
    if result.get("skip_reason"):
        console.print(f"[yellow]skip: {result['skip_reason']}[/yellow]")


@bench_app.command("report")
def bench_report(
    run_id: str | None = typer.Argument(None, help="Benchmark run id; omit for the latest."),
    config: Path | None = typer.Option(None, "--config"),
    scenario: str | None = typer.Option(None, "--scenario"),
    as_json: bool = typer.Option(False, "--json"),
    samples: bool = typer.Option(False, "--samples", help="Include per-iteration samples."),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    """查看已保存的 Benchmark 结果。"""
    settings = _settings_only(config)

    async def _get() -> dict[str, Any] | None:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            runner = BenchmarkRunner(settings, registry=build_registry(), cwd=Path.cwd(), db=db)
            if run_id:
                return await runner.get(run_id)
            rows = await runner.history(scenario_id=scenario, limit=1)
            return rows[0] if rows else None
        finally:
            db.close()

    record = asyncio.run(_get())
    if record is None:
        _fail("benchmark run not found", code=2)
    document = dict(record["result"])
    if not samples:
        document.pop("samples", None)
    if output is not None:
        write_json(output, document)
        if not as_json:
            console.print(f"benchmark report written to [cyan]{output}[/cyan]")
    if as_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
        return
    console.print(
        Panel(
            f"run      {record['run_id']}\n"
            f"scenario {record['scenario_id']}\n"
            f"status   {record['status']}\n"
            f"kind     {record['kind']}\n"
            f"started  {record['started_at']}\n"
            f"git      {record['git_commit'] or '(unknown)'}\n"
            f"app      {record['app_version']}",
            title="Benchmark run",
        )
    )
    _print_benchmark_result(record["result"])
    if samples:
        table = Table(title="Samples")
        table.add_column("#", justify="right")
        table.add_column("status")
        table.add_column("duration_ms", justify="right")
        table.add_column("tokens", justify="right")
        table.add_column("error")
        for sample in record["result"].get("samples") or []:
            table.add_row(
                str(sample["iteration"]),
                sample["status"],
                str(sample["duration_ms"]),
                str(sample.get("tokens", 0)),
                sample.get("error", ""),
            )
        console.print(table)


@bench_app.command("history")
def bench_history(
    config: Path | None = typer.Option(None, "--config"),
    scenario: str | None = typer.Option(None, "--scenario"),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列出历史 Benchmark 运行。"""
    settings = _settings_only(config)

    async def _history() -> list[dict[str, Any]]:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            runner = BenchmarkRunner(settings, registry=build_registry(), cwd=Path.cwd(), db=db)
            return await runner.history(scenario_id=scenario, limit=limit)
        finally:
            db.close()

    rows = asyncio.run(_history())
    if as_json:
        console.print_json(json.dumps(rows, ensure_ascii=False, default=str))
        return
    table = Table(title="Benchmark history")
    table.add_column("run", style="cyan")
    table.add_column("scenario")
    table.add_column("kind")
    table.add_column("status")
    table.add_column("started", style="dim")
    table.add_column("mean_ms", justify="right")
    table.add_column("p95_ms", justify="right")
    for row in rows:
        stats = row["result"]["stats"]
        table.add_row(
            row["run_id"][:18],
            row["scenario_id"],
            row["kind"],
            row["status"],
            row["started_at"][:19],
            str(stats["duration_ms"]["mean"]),
            str(stats["duration_ms"]["p95"]),
        )
    console.print(table)


@bench_app.command("compare")
def bench_compare(
    run_a: str = typer.Argument(..., help="Baseline run id."),
    run_b: str = typer.Argument(..., help="Candidate run id."),
    config: Path | None = typer.Option(None, "--config"),
    threshold: float | None = typer.Option(None, "--threshold", help="Regression threshold %."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """比较同一场景的两次 Benchmark。"""
    settings = _settings_only(config)

    async def _compare() -> dict[str, Any]:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            runner = BenchmarkRunner(settings, registry=build_registry(), cwd=Path.cwd(), db=db)
            return await runner.compare(run_a, run_b, threshold_pct=threshold)
        finally:
            db.close()

    try:
        comparison = asyncio.run(_compare())
    except ValueError as exc:
        _fail(str(exc), code=2)
    if as_json:
        console.print_json(json.dumps(comparison, ensure_ascii=False, default=str))
        return
    console.print(f"verdict: [bold]{comparison['verdict']}[/bold]")
    table = Table(title="Benchmark comparison")
    table.add_column("metric")
    table.add_column("before", justify="right")
    table.add_column("after", justify="right")
    table.add_column("delta", justify="right")
    table.add_column("verdict")
    for name, metric in comparison["metrics"].items():
        color = {
            "regression": "red",
            "improvement": "green",
            "stable": "dim",
        }[metric["verdict"]]
        table.add_row(
            name,
            str(metric["before"]),
            str(metric["after"]),
            f"{metric['delta_pct']:+.2f}%",
            f"[{color}]{metric['verdict']}[/{color}]",
        )
    console.print(table)
    if not comparison["compatible_environment"]:
        console.print(
            "[yellow]environment differs between runs; treat the comparison as indicative[/yellow]"
        )


@bench_app.command("prune")
def bench_prune(
    config: Path | None = typer.Option(None, "--config"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm deletion."),
) -> None:
    """按 [benchmarks] 保留策略删除旧结果。"""
    if not yes:
        _fail("refusing to delete benchmark results without --yes", code=2)
    settings = _settings_only(config)

    async def _prune() -> int:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            runner = BenchmarkRunner(settings, registry=build_registry(), cwd=Path.cwd(), db=db)
            return await runner.prune()
        finally:
            db.close()

    console.print(f"deleted {asyncio.run(_prune())} benchmark run(s)")


@bench_app.command("export")
def bench_export(
    run_id: str = typer.Argument(...),
    output: Path = typer.Option(..., "--output", "-o"),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """导出一次 Benchmark 的完整结果。"""
    settings = _settings_only(config)

    async def _get() -> dict[str, Any] | None:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            runner = BenchmarkRunner(settings, registry=build_registry(), cwd=Path.cwd(), db=db)
            return await runner.get(run_id)
        finally:
            db.close()

    record = asyncio.run(_get())
    if record is None:
        _fail(f"benchmark run not found: {run_id}", code=2)
    write_json(output, record)
    console.print(f"benchmark exported to [cyan]{output}[/cyan]")


# ----------------------------------------------------------------------- admin


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config."),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """创建初始配置文件。"""
    target = config or default_config_path()
    try:
        write_default_config(target, force=force)
    except FileExistsError:
        console.print(f"config already exists: [cyan]{target}[/cyan] (use --force to overwrite)")
        raise typer.Exit(code=0) from None
    console.print(f"created [cyan]{target}[/cyan]")
    console.print("Next: set your API key env var (e.g. DEEPSEEK_API_KEY), then run `bear`.")


@app.command()
def doctor(
    config: Path | None = typer.Option(None, "--config"),
    cwd: Path | None = typer.Option(None, "--cwd", "-C", help="Workspace directory."),
    as_json: bool = typer.Option(False, "--json", help="Emit a structured JSON report."),
    export: Path | None = typer.Option(None, "--export", help="Write the report to a JSON file."),
    active: bool = typer.Option(
        False,
        "--active",
        help="Run bounded connectivity checks (small provider request, MCP startup).",
    ),
    category: str | None = typer.Option(
        None, "--category", help="Only show checks in these comma-separated categories."
    ),
) -> None:
    """统一诊断配置、存储、Provider、MCP、Plugin、Sandbox 与可观测性。"""
    config_path = config or default_config_path()
    try:
        settings = load_settings(config)
    except Exception as exc:
        report = DiagnosticReport(
            app_version=__version__,
            schema_version=SCHEMA_VERSION,
            generated_at=datetime.now(UTC),
            checks=[
                DiagnosticCheck(
                    id="config.load",
                    category="config",
                    status=CheckStatus.FAIL,
                    summary="configuration could not be loaded",
                    detail=SecretRedactor().redact(str(exc))[:1_000],
                    mode=CheckMode.STATIC,
                )
            ],
            config_path=str(config_path),
        )
        _emit_diagnostics(report, as_json=as_json, export=export)
        raise typer.Exit(code=1) from None
    workspace = _resolve_cwd(cwd)
    categories = (
        {item.strip() for item in category.split(",") if item.strip()} if category else None
    )
    try:
        report = asyncio.run(
            DiagnosticsService(
                settings,
                cwd=workspace,
                config_path=config_path,
            ).run(active=active, categories=categories)
        )
    except Exception as exc:
        _fail(f"diagnostics failed: {exc}", code=1)
    _emit_diagnostics(report, as_json=as_json, export=export)
    raise typer.Exit(code=report.exit_code)


def _emit_diagnostics(
    report: DiagnosticReport,
    *,
    as_json: bool,
    export: Path | None,
) -> None:
    payload = report.to_dict()
    if export is not None:
        write_json(export, payload)
        if not as_json:
            console.print(f"report written to [cyan]{export}[/cyan]")
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title="bear doctor")
    table.add_column("check", style="cyan", no_wrap=True)
    table.add_column("state")
    table.add_column("mode", style="dim")
    table.add_column("detail")
    colors = {
        CheckStatus.PASS: "green",
        CheckStatus.WARN: "yellow",
        CheckStatus.FAIL: "red",
        CheckStatus.SKIP: "dim",
    }
    for check in report.checks:
        color = colors[check.status]
        detail = check.summary
        if check.detail:
            detail += f" — {check.detail}"
        table.add_row(
            check.id,
            f"[{color}]{check.status.value}[/{color}]",
            check.mode.value,
            detail,
        )
    console.print(table)
    counts = report.counts
    console.print(
        f"[dim]{counts['pass']} pass, {counts['warn']} warn, "
        f"{counts['fail']} fail, {counts['skip']} skip[/dim]"
    )
    if not report.ok:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------- audit


@audit_app.command("verify")
def audit_verify(
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """验证本地审计哈希链。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _verify() -> AuditVerification:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            return await AuditService(db, enabled=settings.audit.enabled).verify()
        finally:
            db.close()

    verification = asyncio.run(_verify())
    if verification.ok:
        console.print(f"[green]audit chain OK[/green] ({verification.records} records)")
        return
    console.print(
        f"[red]audit chain FAILED[/red] at id={verification.first_bad_id}: {verification.reason}"
    )
    raise typer.Exit(code=1)


@audit_app.command("tail")
def audit_tail(
    config: Path | None = typer.Option(None, "--config"),
    limit: int = typer.Option(20, "--limit", min=1, max=500),
) -> None:
    """显示最近的审计记录。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _tail() -> list[AuditEntry]:
        db = Database(settings.resolved_data_dir() / "bear.db")
        try:
            return await AuditService(db, enabled=settings.audit.enabled).tail(limit)
        finally:
            db.close()

    entries = asyncio.run(_tail())
    table = Table(title="Audit tail")
    table.add_column("id", style="cyan")
    table.add_column("time", style="dim")
    table.add_column("actor")
    table.add_column("action")
    table.add_column("resource")
    for entry in entries:
        table.add_row(
            str(entry.id),
            entry.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            entry.actor,
            entry.action,
            entry.resource or "",
        )
    console.print(table)


# --------------------------------------------------------------------- secrets


async def _secret_manager(settings: Settings) -> tuple[Database, SecretManager]:
    data_dir = settings.ensure_data_dirs()
    db = Database(data_dir / "bear.db")
    manager = SecretManager(
        secret_store_from_config(settings.secrets.backend),
        SecretNameRepository(db),
        SecretRedactor(),
    )
    return db, manager


@secret_app.command("set")
def secret_set(
    name: str = typer.Argument(..., help="Secret name, e.g. deepseek."),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """把密钥写入 OS keyring。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    value = typer.prompt("Secret value", hide_input=True, confirmation_prompt=True)

    async def _set() -> None:
        db, manager = await _secret_manager(settings)
        try:
            if not manager.available:
                raise BearError("secret backend is unavailable")
            await manager.set(name, value)
        finally:
            db.close()

    try:
        asyncio.run(_set())
    except Exception as exc:
        _fail(f"failed to store secret: {exc}", code=1)
    console.print(f"stored secret [cyan]{name}[/cyan]")


@secret_app.command("list")
def secret_list(
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """列出密钥名称(绝不显示值)。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _list() -> list[str]:
        db, manager = await _secret_manager(settings)
        try:
            return await manager.list_names()
        finally:
            db.close()

    names = asyncio.run(_list())
    if not names:
        console.print("[dim]no secrets stored[/dim]")
        return
    for name in names:
        console.print(f"  [cyan]{name}[/cyan]")


@secret_app.command("delete")
def secret_delete(
    name: str = typer.Argument(..., help="Secret name."),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """从 OS keyring 删除密钥。"""
    settings = _load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )

    async def _delete() -> None:
        db, manager = await _secret_manager(settings)
        try:
            await manager.delete(name)
        finally:
            db.close()

    try:
        asyncio.run(_delete())
    except Exception as exc:
        _fail(f"failed to delete secret: {exc}", code=1)
    console.print(f"deleted secret [cyan]{name}[/cyan]")


# ---------------------------------------------------------------------- root


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Show version."),
    config: Path | None = typer.Option(None, "--config", help="Config TOML path."),
) -> None:
    if version:
        console.print(f"bear-agent {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        # Calling through Typer's invoke would pass OptionInfo sentinels for
        # parameters the root callback does not know about. Enter the chat
        # implementation directly with explicit defaults instead.
        chat(
            config=config,
            cwd=None,
            provider=None,
            model=None,
            base_url=None,
            api_key=None,
            permission=None,
            max_turns=None,
            resume=None,
            continue_latest=False,
            yes=False,
        )


if __name__ == "__main__":
    app()
