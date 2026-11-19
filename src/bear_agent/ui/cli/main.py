"""Bear-Agent CLI.

Commands are thin: load settings, build the composition root, subscribe a
renderer, call the runtime. No agent logic lives in the CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from bear_agent import __version__
from bear_agent.audit.service import AuditEntry, AuditService, AuditVerification
from bear_agent.bootstrap import Container, build_container, permission_mode
from bear_agent.config.loader import (
    default_config_path,
    load_settings,
    settings_to_toml,
    write_default_config,
)
from bear_agent.config.models import Settings
from bear_agent.core.errors import BearError
from bear_agent.core.usage import TokenUsage
from bear_agent.policy.approver import AutoApprover, DenyingApprover
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.secrets.store import SecretManager, secret_store_from_config
from bear_agent.sessions.models import SessionRecord
from bear_agent.sessions.service import SessionService
from bear_agent.shell.sandbox.base import SandboxManager
from bear_agent.storage.database import Database
from bear_agent.storage.security_repositories import SecretNameRepository
from bear_agent.tools.builtin import create_builtin_tools
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
config_app = typer.Typer(help="查看配置。")
audit_app = typer.Typer(help="查看安全审计链。")
secret_app = typer.Typer(help="管理 keyring 密钥。")
app.add_typer(sessions_app, name="sessions")
app.add_typer(config_app, name="config")
app.add_typer(audit_app, name="audit")
app.add_typer(secret_app, name="secret")


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


def _fail(message: str, *, code: int = 1) -> None:
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
/session              show current session id and status
/permission <mode>    plan | supervised | auto | unrestricted
/new                  start a new session
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
            console.print(
                Panel(
                    f"id      {record.id}\n"
                    f"cwd     {record.cwd}\n"
                    f"status  {record.status}\n"
                    f"model   {record.model}\n"
                    f"title   {record.title}\n"
                    f"seq     {record.last_seq}",
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
        # shell.env may contain user-supplied secrets; never print it.
        data.get("shell", {}).pop("env", None)
        console.print_json(json.dumps(data, ensure_ascii=False))
    else:
        console.print(settings_to_toml(settings), markup=False)


@config_app.command("path")
def config_path() -> None:
    """输出默认配置路径。"""
    console.print(str(default_config_path()))


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
) -> None:
    """检查配置、存储、Provider、Sandbox 与工具就绪状态。"""
    checks: list[tuple[str, str, str]] = []
    try:
        settings = load_settings(config)
        checks.append(("config", "ok", "loaded"))
    except BearError as exc:
        checks.append(("config", "fail", str(exc)))
        _print_checks(checks)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        checks.append(("config", "fail", str(exc)))
        _print_checks(checks)
        raise typer.Exit(code=1) from None

    checks.append(("python", "ok", platform.python_version()))
    try:
        data_dir = settings.ensure_data_dirs()
        probe = data_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(("data-dir", "ok", str(data_dir)))
    except OSError as exc:
        checks.append(("data-dir", "fail", str(exc)))

    try:
        count, verification = asyncio.run(_doctor_db_checks(settings))
        checks.append(("database", "ok", f"{count} recent session(s) readable"))
        if not settings.audit.enabled:
            checks.append(("audit", "warn", "audit disabled by configuration"))
        elif verification.ok:
            checks.append(("audit", "ok", f"hash chain verified ({verification.records} records)"))
        else:
            checks.append(("audit", "fail", verification.reason))
    except Exception as exc:
        checks.append(("database", "fail", str(exc)))

    secret_store = secret_store_from_config(settings.secrets.backend)
    try:
        settings.validate_ready(secret_store=secret_store)
        provider = settings.provider()
        checks.append(
            ("provider", "ok", f"{settings.agent.default_provider} | {provider.base_url}")
        )
    except Exception as exc:
        checks.append(("provider", "fail", str(exc)))

    if secret_store.available():
        checks.append(("secrets", "ok", f"{settings.secrets.backend} backend available"))
    else:
        checks.append(("secrets", "warn", "keyring unavailable; env/plain references still work"))

    sandbox = SandboxManager(settings.sandbox, Path.cwd())
    description = sandbox.describe()
    try:
        sandbox.ensure_available()
        if settings.sandbox.mode == "off":
            checks.append(("sandbox", "warn", "explicitly disabled; shell is not isolated"))
        else:
            checks.append(
                (
                    "sandbox",
                    "ok",
                    f"{description.get('backend')} mode={description.get('mode')} "
                    f"capabilities={description.get('capabilities')}",
                )
            )
    except Exception as exc:
        checks.append(("sandbox", "warn", f"shell unavailable: {exc}"))

    try:
        tools = create_builtin_tools()
        extra = " + shell" if settings.shell.enabled else ""
        checks.append(("tools", "ok", f"{len(tools)} built-in tools{extra}"))
    except Exception as exc:
        checks.append(("tools", "fail", str(exc)))

    checks.append(
        (
            "claims",
            "ok" if settings.claims.enabled else "warn",
            f"enabled={settings.claims.enabled} ttl={settings.claims.ttl_s:.0f}s",
        )
    )

    _print_checks(checks)
    if any(state == "fail" for _, state, _ in checks):
        raise typer.Exit(code=1)


async def _doctor_db_checks(settings: Settings) -> tuple[int, Any]:
    db = Database(settings.resolved_data_dir() / "bear.db")
    try:
        service = SessionService(db)
        count = len(await service.list_recent(1))
        verification = await AuditService(db, enabled=settings.audit.enabled).verify()
        return count, verification
    finally:
        db.close()


def _print_checks(checks: list[tuple[str, str, str]]) -> None:
    table = Table(title="bear doctor")
    table.add_column("check", style="cyan")
    table.add_column("state")
    table.add_column("detail")
    for name, state, detail in checks:
        style = {"ok": "green", "warn": "yellow"}.get(state, "red")
        table.add_row(name, f"[{style}]{state}[/{style}]", detail)
    console.print(table)


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
