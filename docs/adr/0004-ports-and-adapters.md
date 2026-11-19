# ADR 0004: Ports and adapters; one composition root

- Status: Accepted
- Date: 2026-11-19

## Context

Agent runtimes tend to collapse into a single loop that knows the provider, the
database, the UI and the tools. That makes testing and replacement expensive.
The Phase 0 design explicitly requires decoupling the runtime from providers,
tools and UI.

## Decision

Define ports in the kernel and implement them as adapters:

- `ModelProvider` (providers/base.py)
- `Tool` + `ToolRegistry` (tools/base.py)
- `SessionService` + repositories (sessions, storage)
- `Approver` (policy/approver.py)
- `EventBus` (observability/bus.py)
- `PathGuard` and `PermissionEngine` (policy)

`bootstrap.py` is the only composition root. `ui/` is an outer layer. The kernel
does not import concrete adapters or Rich/Typer.

## Consequences

- Tests use scripted providers and temporary SQLite without network or UI.
- Adding a provider or a storage backend is localized.
- More explicit wiring in `bootstrap.py`; accepted as the cost of clear
  boundaries.
- Dependency cycles are prevented with structural protocols (e.g.
  `policy.engine.ToolLike`) rather than back-imports.

## Alternatives

- Framework-managed DI container: unnecessary for this scale and hides the
  object graph.
- Direct imports inside the runtime: faster to write, impossible to test or
  replace cleanly.