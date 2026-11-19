# Development

## Setup

```bash
git clone https://github.com/huiliyi2025/Bear-Agent.git
cd Bear-Agent
uv sync
```

Python 3.13+ is required. `uv sync` installs runtime and dev dependencies from
`pyproject.toml` / `uv.lock`.

## Quality gates

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest
```

Run a focused test:

```bash
uv run pytest tests/unit/test_storage.py -v
uv run pytest tests/integration/test_runtime.py -k cancel -v
```

Tests use a scripted provider and a temporary data directory. They must never
call a real model, network or the user's `~/.config`.

## Layout

```text
src/bear_agent/
  core/          message parts, events, cancellation, errors, usage, enums
  config/        Settings models, TOML loader, init template
  storage/       Database, migrations, repositories
  sessions/      SessionRecord, SessionService, transcript + leases
  providers/     ModelProvider port, OpenAI-compatible adapter, retry
  tools/         Tool contract, registry, executor, builtin/
  policy/        PermissionEngine, PathGuard, approver port
  context/       system prompt, token estimate, context builder
  runtime/       AgentRuntime, TurnExecutor, ModelCaller, StopPolicy
  observability/ EventBus, EventRecorder
  ui/cli/        Typer commands, Rich renderer, CLI approver
  bootstrap.py   composition root
tests/
  unit/ integration/ security/ e2e/ support.py conftest.py
```

## Dependency rules

- `core` depends on nothing else in the package.
- Kernel modules may import `core`; they must not import `ui`.
- `tools` must not import `runtime`.
- `policy` must not import `tools.base` (it uses a structural `ToolLike`).
- `sessions` must not import `ui` or providers.
- `bootstrap.py` is the only place that constructs concrete adapters.

A quick check:

```bash
rg "from bear_agent\.(ui|runtime)" src/bear_agent/{core,tools,policy,sessions}
```

If a feature needs a new cross-module dependency, add a port in `core` or a
small protocol in the consuming module instead of importing a concrete adapter.

## Adding a tool

1. Create a `BaseModel` for arguments with `Field(description=...)`.
2. Subclass `Tool` and set `name`, `description`, `args_model`, `side_effect`,
   `idempotent`, `concurrency_safe`, `timeout_s`.
3. Implement `async def run(self, args, ctx) -> ToolResult`.
4. Resolve every path through `ctx.path_guard` (`mode="read"` / `"write"`).
5. Return structured errors (`is_error=True`, `error_kind=...`); never raise for
   expected user errors.
6. Register it in `tools/builtin/__init__.py` (or a plugin later).
7. Add unit tests for success, invalid args, denied path and timeout behavior.

Example skeleton:

```python
class MyArgs(BaseModel):
    path: str = Field(description="File path.")

class MyTool(Tool):
    name = "my_tool"
    description = "Does one thing."
    args_model = MyArgs
    side_effect = ToolSideEffect.READ

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, MyArgs)
        path = ctx.path_guard.resolve(args.path, mode="read")
        return ToolResult(content=f"path={path}")
```

## Adding a provider

1. Implement `ModelProvider.stream()` as an async generator of `ModelEvent`
   values (`TextDelta`, `ToolCallStart`, `ToolCallArgumentsDelta`, `UsageEvent`,
   `FinishEvent`).
2. Normalize `input_tokens` to cache-inclusive semantics.
3. Raise `ProviderError` with a category and `retryable` flag.
4. Add a conformance test with recorded SSE/JSON fixtures.
5. Wire it in `providers/registry.py`; never import it from `runtime`.

## Adding a command

CLI commands are thin adapters. Put business logic in a service or runtime,
then have the command load settings, build a container, subscribe a renderer and
call the service. Use `typer.Exit(code=...)` for exit codes.

## Debugging

- `bear doctor` checks config, data directory, database, provider and tools.
- `bear sessions show <id>` prints the transcript and usage.
- `bear run "..." --json` emits every event as NDJSON.
- The SQLite DB is at `<data_dir>/bear.db`; inspect with `sqlite3`:
  `sqlite3 ~/.local/share/bear-agent/bear.db "select * from sessions;"`.
- Event payloads are JSON; `model.request` carries a prompt hash, not the full
  prompt, to keep the log small and avoid leaking content.
- Set `BEAR_DATA_DIR` to isolate experiments.

## Database migrations

- Schema version lives in `schema_meta`.
- Add a new migration block in `storage/migrations.py`, bump
  `SCHEMA_VERSION`, and keep old versions readable.
- Never edit an already released migration in place.
- Migration failures must abort startup and leave a backup; do not silently
  continue.

## Commit hygiene

- Do not commit API keys, `.env` files, local databases or generated artifacts.
- Keep changes scoped to the current phase.
- Fixes should include a regression test.
- Update docs when behavior or scope changes.