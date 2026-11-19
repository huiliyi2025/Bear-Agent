# Roadmap

Phase-based delivery. A phase is complete only when its acceptance checks pass,
tests are green, and the documentation reflects reality.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| P0 | Research and architecture baseline | Done |
| P1 | Kernel foundation | **Done (this release)** |
| P2 | Tool breadth + execution security | Next |
| P3 | Context compaction + checkpoints + recovery | Planned |
| P4 | Durable Task / Plan / Todo engine | Planned |
| P5 | Hooks, plugins, MCP, provider fallback | Planned |
| P6 | Observability, traces, evaluation | Planned |
| P7 | TUI, packaging, release engineering | Planned |
| P8 | Server/Web UI, multi-session, subagents | Optional |

---

## P0 — Research and architecture (done)

- Deep source review of Tianshu-harness (v3.37.4) and broader harness patterns.
- Product positioning, module boundaries, data flow, security model, roadmap.
- Output: design baseline (this repository's docs/ADRs).

## P1 — Kernel foundation (done)

### Delivered

- Python 3.13 / uv project, strict mypy and ruff configuration.
- Config: TOML + `BEAR_*` env + CLI overrides, redacted `config show`.
- Core: provider-neutral messages, events, cancellation, errors, usage.
- SQLite storage with migrations, sessions, events, messages, tool calls,
  model calls, leases.
- Sessions: create/list/show/resume, append-only transcript, lease with epoch
  and heartbeat, interrupted-tool recovery.
- Providers: OpenAI-compatible streaming and non-streaming adapter, SSE tool
  call assembly, usage normalization, retry classification, bounded retries.
- Tools: contract + registry + executor with schema validation, permission
  checks, approval, timeout, truncation, audit events.
- Built-ins: `read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`.
- Policy: plan/supervised/auto/unrestricted matrix; `PathGuard` with symlink,
  traversal, sensitive-file and `.git` protections.
- Context: stable system prompt + project instructions, token estimation,
  turn-grouped budget assembly.
- Runtime: bounded turn loop, stop conditions (final answer, max turns, max tool
  calls, no progress, timeout, cancellation), structured run results.
- CLI: `bear`, `run`, `sessions`, `config`, `init`, `doctor`, `--json`, streaming
  rendering, approval prompt.
- Tests: unit, integration, security, CLI E2E; no real network/model required.
- Docs: README, ARCHITECTURE, ROADMAP, DEVELOPMENT, SECURITY, CONTRIBUTING, ADRs.
- CI workflow for lint, format, typecheck and tests.

### Acceptance evidence

- `uv run pytest` passes (unit + integration + security + CLI E2E).
- `uv run ruff check src tests` and `ruff format --check` pass.
- `uv run mypy src` passes under strict settings.
- A scripted provider can execute a tool call, persist the transcript and
  resume it in a second process.
- Run cancellation is covered by integration tests; a second scenario verifies
  that interrupted `running` tool calls are marked `unknown` and never replayed.
- Path escape, symlink escape, sensitive file and supervised denial tests pass.

### Known limits (deferred by design)

- No shell tool / OS sandbox yet (P2).
- No context summarization, anchor ledger or checkpoint files (P3).
- No Task/Plan/Todo engine; the loop is single-request oriented (P4).
- No hooks/MCP/plugins (P5).
- No trace UI/eval suite (P6).
- TUI is a streaming CLI, not a full-screen app (P7).
- Usage may be zero when a provider does not emit usage in stream mode.

## P2 — Tool breadth + execution security (next)

### Scope

- `bash` tool with timeout, process-group termination, output bounds and
  background jobs.
- Shell command policy (allow/deny prefixes, wrappers, dangerous patterns).
- OS sandbox adapters: macOS Seatbelt, Linux bwrap/landlock, Windows fail-closed
  fallback; Docker optional.
- Approval scopes: once / session / project; approval persistence and resume.
- Secret store: keyring-backed API keys, log/event redaction.
- Hash-chained audit log.
- Cross-session file claims for concurrent runs.
- MCP config/discovery is intentionally P5.

### Acceptance

- Dangerous command matrix triggers the expected decision in every mode.
- Sandboxed command cannot write outside the workspace (where the platform
  supports it); unsupported platforms fail closed with a clear message.
- A denied command is auditable and returns an actionable error to the model.
- Secrets never appear in events, logs or `config show`.
- Concurrent writes to the same file are rejected or serialized.

## P3 — Context compaction + checkpoints + recovery

### Scope

- L1 tool-result pruning, L2 stale-turn summary, L3 structured LLM summary,
  L4 checkpoint/session split.
- Anchor ledger (goal, constraints, decisions, files, errors, verification).
- Artifact store and `recall`/`read_artifact`.
- Checkpoint snapshots before risky operations and at step boundaries.
- Crash recovery from snapshot + event replay.
- Session fork.

### Acceptance

- A long session compacts without losing task state or acceptance criteria.
- Every lossy rewrite has an artifact reference.
- A killed process resumes at a safe point without replaying side effects.
- Summary quality checks cover all anchor categories.

## P4 — Durable Task / Plan / Todo engine

### Scope

- Task, Plan (versioned), Step (dependencies, attempts, acceptance), Todo
  projection.
- Retry, replanning, verification, plan approval, plan mode.
- Task events and progress projection; resume from first incomplete step.
- `todo` and `plan` tools.

### Acceptance

- Multi-step task survives restart and continues from the first incomplete step.
- Failed steps retry per policy, then replan or block with a reason.
- Task completion requires evidence; unverified completion is explicit.
- Todo and Plan never diverge.

## P5 — Extensibility

- Interceptor/observer hooks, external command hooks, project trust.
- Python entry-point plugins and examples.
- MCP client, tool namespacing, capability policy, health/restart.
- Provider fallback, multiple providers, capability matrix.

## P6 — Observability and evaluation

- Trace service and CLI (`bear trace`, `stats`, `usage`, `replay`).
- Cost model, per-session/task aggregates, export.
- Benchmark suite/runner/report; reproducible fixtures.

## P7 — UX and distribution

- Full-screen TUI (prompt_toolkit/Textual), plan/todo panels, diff preview.
- Installer (`uv tool`, wheel), release workflow, upgrade/migration.
- Complete user, provider, tool and security guides.

## P8 — Optional

- HTTP/SSE server, Web UI, multi-session service.
- Subagent delegation and parallel execution.
- Remote/MCP ecosystem integrations.

---

## Definition of done (every phase)

1. Feature implemented in code, not only documented.
2. Unit + integration tests for new behavior; security tests for new boundaries.
3. `ruff`, `mypy` and full test suite pass.
4. Docs and this roadmap updated; limitations stated honestly.
5. No regression in existing phase acceptance.