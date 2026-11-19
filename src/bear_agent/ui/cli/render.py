"""Event renderer for the terminal.

The renderer is an EventBus subscriber; it has no access to runtime internals.
JSON mode emits one NDJSON object per event for scripts and CI.
"""

from __future__ import annotations

import json

from rich.console import Console

from bear_agent.core.events import Event, EventType


class Renderer:
    def __init__(self, *, json_mode: bool = False, console: Console | None = None) -> None:
        self.json_mode = json_mode
        self.console = console or Console()
        self._text_started = False

    async def __call__(self, event: Event) -> None:
        if self.json_mode:
            print(json.dumps(event.to_json(), ensure_ascii=False), flush=True)
            return
        if event.type == EventType.MODEL_TEXT_DELTA:
            text = str(event.payload.get("text") or "")
            if text:
                self._text_started = True
                self.console.print(text, end="", markup=False, highlight=False, soft_wrap=True)
        elif event.type == EventType.TOOL_STARTED:
            if self._text_started:
                self.console.print()
                self._text_started = False
            name = event.payload.get("name", "?")
            self.console.print(f"[cyan]> {name}[/cyan] ...", end=" ")
        elif event.type == EventType.TOOL_FINISHED:
            status = event.payload.get("status")
            if status == "completed":
                self.console.print("[green]ok[/green]")
            else:
                error = event.payload.get("status", "failed")
                self.console.print(f"[red]failed: {error}[/red]")
        elif event.type == EventType.MODEL_RETRY:
            self.console.print(
                f"[yellow]retrying provider: {event.payload.get('reason', '')}[/yellow]"
            )
        elif event.type in (
            EventType.RUN_COMPLETED,
            EventType.RUN_CANCELLED,
            EventType.RUN_FAILED,
        ):
            if self._text_started:
                self.console.print()
                self._text_started = False
            status = event.payload.get("status", event.type.value)
            reason = event.payload.get("reason", "")
            if status == "completed":
                self.console.print(f"[dim]run completed - {reason}[/dim]")
            elif status == "cancelled":
                self.console.print(f"[yellow]run cancelled - {reason}[/yellow]")
            else:
                self.console.print(f"[red]run {status} - {reason}[/red]")

    def summary(self, status: str, turns: int, usage: dict[str, int], reason: str) -> None:
        if self.json_mode:
            print(
                json.dumps(
                    {
                        "type": "result",
                        "status": status,
                        "turns": turns,
                        "usage": usage,
                        "reason": reason,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        self.console.print(
            f"[dim]{status} | {turns} turn(s) | "
            f"{usage.get('input_tokens', 0)} in / "
            f"{usage.get('output_tokens', 0)} out tokens[/dim]"
        )
