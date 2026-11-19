# ADR 0005: Permissions are enforced at the execution layer

- Status: Accepted
- Date: 2026-11-19

## Context

Prompt instructions such as "do not read .env" are suggestions to a
probabilistic model. A malicious repository or a prompt injection can override
them. The harness needs deterministic, testable boundaries.

## Decision

Enforce permissions in the tool executor and path guard, not only in the system
prompt:

- `PermissionEngine` returns allow/ask/deny from the tool's declared side effect
  and the session mode.
- `PathGuard` resolves and canonicalizes every path before a tool sees it.
- Sensitive files and `.git` writes are denied in code.
- The executor validates arguments, applies approval, enforces timeouts and
  persists every decision as an event.
- Security controls fail closed and return actionable errors.

## Consequences

- The model cannot override a deny by writing a persuasive prompt.
- New tools must declare side effects and use the path guard.
- The policy layer is independently testable (matrix and path corpus).
- Some legitimate actions need explicit approval; this is intentional.

## Alternatives

- Prompt-only restrictions: rejected; not enforceable.
- Blocking at the UI layer: rejected; headless and API surfaces would bypass it.
- Post-hoc auditing only: rejected; damage is already done.