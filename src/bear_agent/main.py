"""Bear Agent CLI: wiring for Settings + LLM + Tools + Policy + REPL."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bear_agent.agent.loop import AgentLoop, MaxStepsExceeded
from bear_agent.agent.session import Session
from bear_agent.config import Settings
from bear_agent.llm.openai_compat import OpenAIChatClient
from bear_agent.policy.permissions import PermissionPolicy
from bear_agent.tools.base import Tool
from bear_agent.tools.builtin import builtin_tools
from bear_agent.tools.registry import ToolRegistry

BANNER = """\
================================
  Welcome to Bear Agent Runtime
  self-evolving harness agent
================================
"""

HELP_TEXT = """\
commands:
  /plan on|off   toggle plan mode (mutations are denied while on)
  /tools         list registered tools
  /help          show this help
  /quit          exit
"""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bear-agent",
        description="Bear Agent — a self-evolving harness agent runtime.",
    )
    parser.add_argument("--model", help="LLM model name (env BEAR_MODEL).")
    parser.add_argument(
        "--base-url",
        help="OpenAI-compatible base URL, e.g. https://api.deepseek.com/v1 (env BEAR_BASE_URL).",
    )
    parser.add_argument("--api-key", help="API key (env BEAR_API_KEY).")
    parser.add_argument(
        "--working-dir",
        type=Path,
        help="Directory tools operate in (env BEAR_WORKING_DIR).",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        help="Max Agent Loop steps per turn (env BEAR_MAX_STEPS).",
    )
    parser.add_argument(
        "--temperature", type=float, help="Sampling temperature (env BEAR_TEMPERATURE)."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        dest="request_timeout",
        help="HTTP timeout in seconds (env BEAR_REQUEST_TIMEOUT).",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Start in plan mode: mutations are automatically denied.",
    )
    return parser.parse_args(argv)


def settings_from_args(args: argparse.Namespace) -> Settings:
    values: dict[str, Any] = {}
    for field in ("model", "base_url", "api_key"):
        value = getattr(args, field, None)
        if value is not None:
            values[field] = value
    for field in ("max_steps", "temperature", "request_timeout"):
        value = getattr(args, field, None)
        if value is not None:
            values[field] = value
    if args.working_dir is not None:
        values["working_dir"] = args.working_dir
    if args.plan:
        values["plan_mode"] = True
    return Settings(**values)


def _make_cli_confirm() -> Callable[[Tool, dict[str, Any]], bool]:
    def confirm(tool: Tool, arguments: dict[str, Any]) -> bool:
        print(f"\n[permission] request to run `{tool.name}` with arguments:")
        print(json.dumps(arguments, ensure_ascii=False, indent=2))
        answer = input("Proceed? [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    return confirm


def _emitter(event: str, payload: dict[str, Any]) -> None:
    if event == "step":
        print(f"  · step {payload['step']} ...", flush=True)
    elif event == "decision":
        decision = payload.get("decision") or "?"
        mark = {"allow": "ok", "confirm": "?", "deny": "blocked"}.get(decision, "?")
        print(f"    → {payload.get('tool')}: {mark} ({decision})", flush=True)
    elif event == "tool_result":
        ok = payload.get("ok")
        print(f"    · {payload.get('tool')}: returned ok={ok}", flush=True)


def repl(
    settings: Settings,
    registry: ToolRegistry,
    llm: OpenAIChatClient,
    policy: PermissionPolicy,
) -> None:
    session = Session()
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        policy=policy,
        max_steps=settings.max_steps,
        emit=_emitter,
    )
    print(BANNER)
    print(f"model     : {settings.model}")
    print(f"base_url  : {settings.base_url}")
    print(f"working   : {settings.working_dir.resolve()}")
    print(f"tools     : {', '.join(registry.names())}")
    print(f"plan_mode : {'ON' if policy.plan_mode else 'OFF'}")
    print(HELP_TEXT)

    while True:
        try:
            line = input("bear> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.startswith("/"):
            if line in ("/quit", "/exit"):
                break
            if line in ("/plan on",):
                policy.plan_mode = True
                print("[plan mode ON] mutations will be denied")
                continue
            if line == "/plan off":
                policy.plan_mode = False
                print("[plan mode OFF]")
                continue
            if line == "/tools":
                for name in registry.names():
                    tool = registry.get(name)
                    permission = tool.permission if tool else "?"
                    description = tool.description if tool else ""
                    print(f"  - {name} ({permission}): {description}")
                continue
            if line in ("/help", "/h"):
                print(HELP_TEXT)
                continue
            print(f"unknown command: {line}")
            continue

        try:
            answer = loop.run(session, line)
        except MaxStepsExceeded as exc:
            print(f"\n[!] {exc}\n")
            continue
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive
            print(f"\n[!] loop error: {type(exc).__name__}: {exc}\n")
            continue
        print(f"\nassistant> {answer}\n")


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = settings_from_args(args)

    if not settings.is_local_provider and not settings.api_key:
        print(
            "error: no API key. Set BEAR_API_KEY or pass --api-key; "
            "or point --base-url at a local provider (Ollama / localhost).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    registry = ToolRegistry()
    for tool in builtin_tools(settings.working_dir):
        registry.register(tool)

    llm = OpenAIChatClient(
        model=settings.model,
        base_url=settings.base_url,
        api_key=settings.api_key,
        timeout=settings.request_timeout,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
    )
    policy = PermissionPolicy(
        plan_mode=settings.plan_mode, confirm_fn=_make_cli_confirm()
    )

    try:
        repl(settings, registry, llm, policy)
    finally:
        llm.close()


if __name__ == "__main__":
    main()
