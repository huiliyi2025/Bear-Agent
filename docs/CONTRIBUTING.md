# Contributing

Thanks for helping build Bear-Agent. This project is developed in phases; keep
changes aligned with the current phase and do not claim deferred features as
done.

## Before you start

- Read `docs/ARCHITECTURE.md`, `docs/ROADMAP.md` and `docs/SECURITY.md`.
- Check the current phase in `docs/ROADMAP.md`.
- For a bug fix, add a regression test first.
- For a security change, include a test that demonstrates the boundary.

## Development setup

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

## Pull requests

1. Keep the change scoped; avoid unrelated refactors.
2. Add or update tests. New behavior without tests is not complete.
3. Update docs and `ROADMAP.md` when scope or behavior changes.
4. Do not commit `.env`, API keys, local databases, generated caches or
   user-specific paths.
5. Do not weaken a security boundary to make a test pass. Explain exceptions.
6. Do not add a dependency without a concrete need and a note in the PR.

## Code style

- Python 3.13, `from __future__ import annotations`, type hints on public APIs.
- Pydantic v2 models for data crossing a boundary; dataclasses for internal
  value objects where validation is unnecessary.
- Async for I/O; never block the event loop with filesystem/network work in a
  coroutine without `asyncio.to_thread` or an injected adapter.
- Explicit SQL over ORM magic; transactions around multi-row invariants.
- Comments explain *why* and invariants, not restate the code.
- Error messages should be actionable and safe (no secrets, no stack traces to
  the model by default).

## Adding a tool

See `docs/DEVELOPMENT.md#adding-a-tool`. Required tests:

- success path;
- invalid arguments;
- path denied (if it touches the filesystem);
- timeout/cancellation behavior;
- event/tool-call persistence if side-effecting.

## Adding a provider

See `docs/DEVELOPMENT.md#adding-a-provider`. Providers must:

- implement the `ModelProvider` port;
- normalize usage;
- classify retryable failures;
- never leak API keys in errors;
- pass a fixture-based conformance test.

## Architecture rules

- `core` has no harness dependencies.
- `ui` may depend inward; the kernel must never import `ui`.
- `tools` must not import `runtime`.
- `policy` must not import `tools.base` (use the `ToolLike` protocol).
- Adapters are constructed only in `bootstrap.py`.
- New cross-layer dependencies require an ADR if they change a boundary.

## Commit messages

Use a short imperative subject, then a body explaining why and what was
verified. Example:

```text
Add bounded tool executor retries

Tool timeouts now return a structured error and persist a failed tool_call row.
Tests cover timeout, denial and unknown-tool paths.
```

## Reporting issues

Include the command, config shape (redacted), expected vs actual behavior, and
the relevant event/session id. Never paste API keys or private source code.

## License

By contributing, you agree that your contributions are licensed under
Apache-2.0 (see `LICENSE`).