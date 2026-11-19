# Architecture

> Status: Phase 1 (kernel foundation). This document describes what is
> implemented today and the boundaries later phases must respect.

## Goals

Bear-Agent is a local-first agent harness with four non-negotiable properties:

1. **Bounded execution** — every run has turn, tool-call, time and token limits.
2. **Durable state** — material actions and state transitions are persisted.
3. **Execution-layer safety** — permissions and paths are enforced in code,
   not requested in a prompt.
4. **Replaceable edges** — models, storage, tools and UI are ports/adapters.

Non-goals for Phase 1: hosted multi-tenant service, distributed workers,
graphical workflow builder, model training.

## Layers

```text
+------------------------------------------------------------------+
| Interfaces: CLI / headless NDJSON / future HTTP+SSE               |
+------------------------------------------------------------------+
| Application: Container (composition root), SessionService         |
+------------------------------------------------------------------+
| Kernel: Runtime - TurnExecutor - Context - Tools - Policy         |
|         Sessions - Events - Providers (port)                      |
+------------------------------------------------------------------+
| Adapters: OpenAI-compatible HTTP, SQLite, OS filesystem, Rich CLI |
+------------------------------------------------------------------+
```

Dependency rule:

- `core` imports nothing from the harness.
- Kernel modules import `core` and ports, not UI or concrete adapters.
- `bootstrap.py` is the only composition root. It wires SQLite, the provider,
  tools, policy, context and runtime.
- `ui/` depends on the application/container; the kernel never imports `ui/`.
- `policy/path_guard.py` depends on `core` only; tools receive a guard through
  `ToolContext` instead of importing policy.

Enforced examples:

- `policy.engine` uses a structural `ToolLike` protocol, not `tools.base.Tool`,
  so policy and tools do not import each other.
- `storage.repositories` imports the session record (adapter depends inward).
- `sessions.service` does not know the event bus; a `publish` callback is
  injected by the caller when live session-created events are needed.

## Runtime flow

```text
CLI / run command
  -> Container
  -> AgentRuntime.run(session_id, input)
       acquire in-process lock
       acquire SQLite session lease (owner + epoch + expiry)
       start lease heartbeat
       append user message + message.user event (one transaction)
       loop:
         check cancellation / wall clock / turn budget
         TurnExecutor.execute:
           load transcript
           ContextBuilder.build (system + project instructions + history + budget)
           ModelCaller.stream (provider events -> assistant message + usage)
           persist assistant message + model.response event
           if tool calls:
             ToolExecutor per call:
               persist tool_call running
               validate args
               policy decision -> optional approval
               execute with timeout
               persist result + tool.call event
             persist tool result message
         StopPolicy.evaluate
       finally:
         mark interrupted tool calls unknown
         release lease
         session idle
         persist run.completed / run.cancelled / run.failed
```

Key invariants:

- A visible message always has a corresponding event (same transaction).
- A tool call is marked `running` before execution and terminal afterwards.
- A crash leaves `running` tools as `unknown`; they are never silently replayed.
- The assistant tool-call message is persisted before the tool side effect.
- A run ends with a structured `RunStatus`, never an ambiguous silence.

## Modules

| Module | Responsibility | Does not |
|---|---|---|
| `core` | Message parts, events, cancellation, errors, usage, modes | I/O |
| `config` | TOML + env + CLI override merging | Start the agent |
| `storage` | SQLite schema, transactions, repositories | Business decisions |
| `sessions` | Session record, transcript, leases | Model calls |
| `providers` | Provider port, OpenAI-compatible adapter, retry classification | Tool execution |
| `tools` | Tool contract, registry, validation, executor, built-ins | Permission policy |
| `policy` | Permission matrix, path containment, sensitive files, approvals | Execute tools |
| `context` | Stable system prompt, token budget, history grouping | Persist state |
| `runtime` | Run/turn orchestration, model aggregation, stop rules | Provider-specific wire logic |
| `observability` | Event bus, recorder, persisted-vs-ephemeral policy | Change run behavior |
| `ui/cli` | Typer commands, Rich rendering, approval prompt | Agent loop logic |

## Message model

Internal messages are provider-neutral:

```python
Message(role, parts=[TextPart | ToolCallPart | ToolResultPart])
```

The OpenAI adapter converts to `chat/completions` payloads. Adding another
provider means adding an adapter, not changing the runtime. Tool-call arguments
are streamed as fragments and validated at the end; an incomplete call is
returned with `arguments_error` and the executor refuses to run it.

## Events

Events are the observable facts of a session. Facts are persisted with a
per-session monotonic `seq`; deltas (streaming text/thinking, tool progress) are
published live only.

```text
session.created
run.started / run.completed / run.cancelled / run.failed
turn.started / turn.completed
message.user / message.assistant / message.tool
model.request / model.response / model.retry
tool.call.started / tool.call.completed / tool.call.failed
policy.decision / approval.requested / approval.decided
context.built / context.truncated
model.text.delta / model.thinking.delta / tool.started / tool.finished
```

The event bus isolates subscribers: a failing UI/telemetry handler cannot break
a run.

## Persistence

SQLite (WAL) is the source of truth. Tables in Phase 1:

- `schema_meta` — migration version.
- `sessions` — cwd, title, status, model/provider, `last_seq`, lease fields.
- `events` — append-only facts.
- `messages` — internal message JSON, ordered per session.
- `tool_calls` — running/completed/failed/unknown with args and result summary.
- `model_calls` — token usage and latency.
- `artifacts` — reserved for large outputs (Phase 3).
- `audit_log` — reserved for the hash-chained audit trail (Phase 2).

All multi-row mutations use explicit `BEGIN IMMEDIATE` transactions. Message
rows and their message events are written together. Tool-call rows are updated
transactionally through the repository.

## Leases and concurrency

- One active run per session: an in-process `asyncio.Lock` plus a SQLite lease.
- Lease owner is `pid-<pid>-<run_id>` with an epoch, expiry and heartbeat.
- A second run fails fast with "already running" rather than interleaving.
- The epoch is enforced for renew/release in Phase 1. Fencing *every* write
  path and cross-session file claims are Phase 2/3 hardening; do not run two
  writers against the same session on one database yet.

## Context management (Phase 1 scope)

The builder keeps the system prompt and project instructions stable and groups
history by user turn, so an assistant tool-call message is never separated from
its tool results. When the budget is exceeded, the oldest complete turns are
dropped first; only the newest turn's tool results may be truncated. The user's
own latest message is never silently truncated.

Deferred to Phase 3: LLM summarization, anchor ledger, artifact-backed
recovery, prefix-cache-aware compaction, and checkpoint/session split.

## Security (Phase 1 scope)

Implemented:

- permission modes: plan, supervised, auto, unrestricted;
- deny-on-plan for every side effect;
- approval for writes in supervised mode;
- workspace containment via `PathGuard` (including symlink escapes, `..`,
  non-existent tails, Windows drive prefixes, NTFS data streams);
- sensitive-file denial (`.env`, credentials, private keys, `.npmrc`,
  `.netrc`, `.git-credentials`, secrets/token files);
- `.git` write denial;
- JSON-schema argument validation and executor timeouts;
- no shell tool yet.

Deferred: OS sandbox, shell command policy, secret store/keyring, audit chain,
SSRF controls, MCP trust, prompt-injection taint tracking. See `SECURITY.md`.

## UI boundary

The CLI subscribes to the event bus. It does not read the database directly for
live execution, and the runtime never calls `rich` or `typer`. Headless `--json`
emits NDJSON, which is the same event vocabulary a future HTTP/SSE surface will
use.

## Testing

- Unit: config, messages, events, policy matrix, path guard, context budget,
  provider SSE parsing/retries.
- Integration: real SQLite + scripted provider + fake tools, covering tool
  calls, denial, max turns, no progress, cancellation, concurrent-run rejection
  and resume.
- E2E: Typer CLI with a scripted provider, NDJSON, session listing/showing.

No test requires a real model or network.