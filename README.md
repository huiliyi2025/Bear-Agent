# Bear-Agent

A local-first, recoverable, extensible agent harness.

Bear-Agent turns a model into a bounded, observable worker: every tool call is
permission-checked, every material action is persisted, and every run ends with
a structured stop reason. It is built as a real engineering foundation, not a
single-file agent demo.

## Status

**Phase 1 (kernel foundation) is implemented.** The current release can:

- run an async agent loop over an OpenAI-compatible streaming endpoint;
- validate and execute built-in file/search tools;
- enforce workspace containment, sensitive-file protection and permission modes;
- persist sessions, messages, events, tool calls and token usage in SQLite;
- resume a session with its transcript and usage history;
- stream text and tool status to a CLI, or emit NDJSON for scripts;
- cancel a run and recover cleanly from interrupted tool calls.

Not implemented yet (tracked in [ROADMAP.md](docs/ROADMAP.md)): shell execution,
OS sandbox, context compaction, the durable task engine, checkpoints, hooks,
MCP, plugins, subagents and the full TUI.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- An OpenAI-compatible chat-completions endpoint (OpenAI, DeepSeek, Qwen,
  Ollama, vLLM, a relay, ...)

## Install

```bash
git clone https://github.com/huiliyi2025/Bear-Agent.git
cd Bear-Agent
uv sync
```

Run the CLI from the repository:

```bash
uv run bear --help
```

Install the `bear` command onto your PATH:

```bash
uv tool install .
bear --help
```

## Quickstart

```bash
bear init                 # writes ~/.config/bear-agent/config.toml
export DEEPSEEK_API_KEY=...
bear doctor               # checks config, storage, provider and tools
bear                      # interactive session
```

One-shot mode:

```bash
bear run "Read README.md and summarize the project in 5 bullets"
bear run "..." --json     # NDJSON events for scripts / CI
```

Override provider settings without editing the config:

```bash
bear run "hello" \
  --base-url https://api.deepseek.com/v1 \
  --model deepseek-chat \
  --api-key "$DEEPSEEK_API_KEY"
```

## Configuration

The config is TOML. Precedence: CLI flags > `BEAR_*` environment variables >
config file > built-in defaults.

```toml
[agent]
default_provider = "deepseek"
default_model = "deepseek-chat"
max_turns = 30
permission_mode = "auto"   # plan | supervised | auto | unrestricted

[providers.deepseek]
type = "openai_compat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
models = ["deepseek-chat", "deepseek-reasoner"]
```

Useful environment variables:

| Variable | Meaning |
|---|---|
| `BEAR_CONFIG` | Config file path |
| `BEAR_PROVIDER` / `BEAR_MODEL` | Override provider / model |
| `BEAR_BASE_URL` / `BEAR_API_KEY` | Override the default provider |
| `BEAR_DATA_DIR` | Move SQLite DB and artifacts |
| `BEAR_PERMISSION_MODE` | `plan`, `supervised`, `auto`, `unrestricted` |

Secrets are never printed by `bear config show`. Prefer `api_key_env` over a
literal key in the file.

## Commands

```text
bear                         interactive session
bear run <prompt>            one headless run
bear run <prompt> --json     NDJSON event stream
bear sessions list           recent sessions
bear sessions show <id>      transcript + usage
bear config show             effective config (redacted)
bear config path             default config location
bear init                    create starter config
bear doctor                  readiness checks
```

Interactive slash commands: `/help`, `/tools`, `/session`, `/permission <mode>`,
`/new`, `/exit`.

## Permission modes

| Mode | Reads | Workspace writes | Future exec |
|---|---|---|---|
| `plan` | allow | deny | deny |
| `supervised` | allow | ask | ask |
| `auto` (default) | allow | allow | ask |
| `unrestricted` | allow | allow | allow |

Even in `unrestricted`, path containment, sensitive-file denial and `.git`
write protection remain active. Deny rules always win.

## Data and privacy

- SQLite DB: `~/.local/share/bear-agent/bear.db` (Linux),
  `%LOCALAPPDATA%\bear-agent` (Windows), overridable with `BEAR_DATA_DIR`.
- Messages, events, tool-call metadata and token usage are persisted locally.
- No telemetry leaves your machine. The provider receives the conversation
  needed to answer the request.
- Do not put credentials in the workspace; the path guard blocks common
  credential files, but that is not a substitute for good hygiene.

See [SECURITY.md](docs/SECURITY.md) for the threat model and current limits.

## Development

```bash
uv sync
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest
```

See [DEVELOPMENT.md](docs/DEVELOPMENT.md) for the architecture, adding tools and
debugging. The design baseline is in [ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[ROADMAP.md](docs/ROADMAP.md).

## Project layout

```text
src/bear_agent/
  core/          events, messages, cancellation, usage, shared enums
  config/        TOML + env settings
  storage/       SQLite, migrations, repositories
  sessions/      session records, transcript, leases
  providers/     model-provider port + OpenAI-compatible adapter
  tools/         tool contract, registry, executor, built-ins
  policy/        permission matrix, path guard, approvals
  context/       prompt, token budget, working-context assembly
  runtime/       agent loop, turn executor, stop policy
  observability/ event bus + recorder
  ui/cli/        Typer/Rich CLI, renderer, approval prompt
```

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](docs/CONTRIBUTING.md) first.
The project uses a phase-based roadmap; keep changes aligned with the current
phase and include regression tests for fixes.

## License

Apache-2.0. See [LICENSE](LICENSE).