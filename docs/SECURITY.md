# Security

> Phase 1 scope. This document states what is enforced today and what is not.
> Do not treat deferred controls as implemented.

## Threat model

Bear-Agent assumes the model can be wrong, manipulated, or actively adversarial:

- prompt injection from repository files, web content or tool output;
- attempts to read credentials or write outside the workspace;
- destructive or irreversible actions;
- malicious MCP servers or plugins (Phase 5);
- resource exhaustion (turns, tokens, disk, processes);
- secret leakage through logs, events or model context.

The runtime, not the prompt, is the trust boundary.

## Implemented controls (Phase 1)

### Permission modes

| Mode | Reads | Workspace writes | Exec/network |
|---|---|---|---|
| `plan` | allow | deny | deny |
| `supervised` | allow | ask | ask |
| `auto` | allow | allow | ask |
| `unrestricted` | allow | allow | allow |

- Deny wins over every allow rule.
- `plan` blocks all side effects, including writes.
- Approval is requested through the `Approver` port; headless runs without a TTY
  use a denying approver.
- Approval decisions are persisted as events.

### Path containment

`PathGuard` resolves and canonicalizes paths before any file operation:

- blocks `..` traversal and absolute paths outside the workspace;
- resolves symlinks (including the nearest existing ancestor for new files) and
  blocks symlink escapes;
- preserves Windows drive prefixes and strips NTFS alternate data streams;
- blocks writes inside `.git`;
- blocks sensitive files: `.env` (except templates), `credentials.*`, SSH
  private keys, `*.pem`, `*.key`, `.npmrc`, `.netrc`, `.git-credentials`,
  `secrets.*`, `tokens.*`;
- returns an actionable error rather than silently redirecting.

### Tool execution

- JSON-schema validation before execution; invalid arguments never reach a tool.
- Per-tool timeout; a timed-out tool returns a structured error.
- Unknown tools and incomplete tool-call arguments are refused.
- Tool output is truncated to a bounded size with an explicit marker.
- Every tool call is persisted with `running` before execution and a terminal
  status after; a crash marks it `unknown` and it is never silently replayed.
- Tool input/result summaries are recorded as events; full file content is not
  persisted in event payloads.

### Prompt and context

- Tool output is treated as untrusted data; the system prompt says so.
- Environment and project instructions are injected as context, not as
  executable authority.
- The model cannot change the tool catalog, permission mode or path roots at
  runtime.

### Data

- Session data is stored locally in SQLite and artifacts; no telemetry is sent
  to the project maintainers.
- The provider receives the messages needed for the request. Do not put secrets
  in the conversation.
- API keys are read from the environment or config; `bear config show` redacts
  them.

## Not yet implemented (tracked for later phases)

| Control | Phase | Current risk |
|---|---|---|
| Shell tool and command policy | P2 | No shell execution exists yet, so the risk is absent, not mitigated |
| OS sandbox (Seatbelt/bwrap/Job Object) | P2 | No process isolation for tools |
| Keyring-backed secret store | P2 | Keys live in env/config |
| Hash-chained audit log | P2 | Events exist, but the audit table is not yet chained |
| Log/event redaction of secrets | P2 | Avoid putting secrets in prompts/workspace |
| MCP trust and capability policy | P5 | MCP is not implemented |
| Prompt-injection taint tracking | P3/P5 | Untrusted output is not tracked as dataflow |
| Resource limits beyond turns/tokens/time | P2 | No process/memory quotas |
| Encrypted database at rest | Later | SQLite is plaintext on disk |

## Operational guidance

- Use `supervised` or `plan` mode when exploring an untrusted repository.
- Do not store API keys in the project; use environment variables or a secret
  manager.
- Keep `BEAR_DATA_DIR` on a trusted, access-controlled filesystem.
- Review tool calls and approval prompts; do not auto-approve in untrusted
  contexts.
- Treat model output and tool output as untrusted.
- Back up the data directory before upgrades that run migrations.

## Reporting

Please report security issues privately to the maintainers rather than opening
a public issue. Include a minimal reproduction, the affected version, and the
impact. Do not include live credentials.

## Security invariants for contributors

1. Never bypass `PathGuard` for a file operation.
2. Never execute a tool call with unvalidated or incomplete arguments.
3. Never let a tool or model output mutate the permission mode or tool catalog.
4. Never log secrets or full credential-file content.
5. Never mark a side effect as completed before it is durably recorded.
6. Adding a new side-effecting tool requires a security test.
7. A security control must fail closed, and its error must be actionable.