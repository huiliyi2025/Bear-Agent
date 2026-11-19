# ADR 0001: Python 3.13 + asyncio for the kernel

- Status: Accepted
- Date: 2026-11-19

## Context

The repository started as a Python skeleton. The harness is I/O-bound (model
streams, tools, SQLite) and needs fast iteration, a mature ecosystem and easy
installation for developers. A TypeScript implementation would mirror the
reference project but would require rewriting the existing code and toolchain.

## Decision

Use Python 3.13, asyncio, Pydantic v2, httpx and uv. Keep the core
provider-neutral and UI-agnostic. Defer any performance-sensitive native
component until profiling proves it necessary.

## Consequences

- Fast development and strong typing via mypy/Pydantic.
- Packaging via `uv tool install` / wheels; no Node runtime requirement.
- CPU-bound work must be offloaded to threads/processes; the runtime is designed
  for I/O concurrency, not parallel CPU.
- A future single-binary distribution can use PyInstaller/Nuitka without
  changing the architecture.

## Alternatives

- TypeScript (Tianshu stack): rejected for migration cost; revisit only if the
  project needs browser-native extensions or a Node-only dependency.
- Rust: rejected for iteration speed at this stage.