# Changelog

All notable changes are recorded here. The project follows a phase-based
roadmap; versions are semantic once a public release is cut.

## [0.1.0] — 2026-11-19 — Phase 1: kernel foundation

### Added

- Async agent runtime with bounded turns, structured stop reasons, cancellation,
  session locks and SQLite leases.
- Provider-neutral message model and event vocabulary.
- OpenAI-compatible streaming/non-streaming provider adapter, SSE tool-call
  assembly, usage normalization and retry classification.
- SQLite persistence: sessions, events, messages, tool calls, model calls,
  schema migrations.
- Session create/list/show/resume and interrupted-tool recovery.
- Tool contract, registry, executor with argument validation, permission
  decisions, approvals, timeouts and result truncation.
- Built-in tools: `read_file`, `write_file`, `edit_file`, `list_dir`, `glob`,
  `grep`.
- Permission modes (`plan`, `supervised`, `auto`, `unrestricted`) and
  `PathGuard` for traversal, symlinks, sensitive files and `.git`.
- Context builder with stable system/project prompt, token estimation and
  turn-grouped budget assembly.
- CLI: `bear`, `run`, `sessions`, `config`, `init`, `doctor`, `--json`,
  streaming renderer and approval prompt.
- Unit, integration, security and CLI E2E tests; ruff, mypy, pytest CI.

### Known limits

- No shell tool, OS sandbox, secret store, audit chain or cross-session file
  claims (Phase 2).
- No compaction, anchor ledger, artifacts or checkpoints (Phase 3).
- No Task/Plan/Todo engine (Phase 4).
- No hooks, MCP or plugins (Phase 5).
- Token usage may be zero when a provider omits stream usage.
- TUI is a streaming CLI, not a full-screen application.