# ADR 0003: Phase 1 scope — safe file tools before shell tools

- Status: Accepted
- Date: 2026-11-19

## Context

Phase 0 defined P1 as the kernel and P2 as tools + security. Shipping a usable
kernel requires write operations, but shell execution and OS sandbox support
are much larger security surfaces. Deferring all writes would make P1 a read-only
demo and force a premature redesign of the tool pipeline.

## Decision

P1 includes `read_file`, `write_file`, `edit_file`, `list_dir`, `glob` and
`grep`, together with `PathGuard` and the permission matrix. Shell execution,
OS sandbox, approval scopes, secret store and audit chaining are deferred to P2.
No side-effecting tool may bypass `PathGuard`, even in `unrestricted` mode.

## Consequences

- P1 is genuinely usable for repository analysis and safe edits.
- The tool executor, policy matrix and path guard are exercised by real tools.
- `bash` and process isolation remain a known risk, but the capability is not
  exposed yet, so there is no claim of mitigation.
- P2 can add execution without restructuring the kernel.

## Alternatives

- Read-only P1: lower risk, but a weaker foundation and a likely redesign of
  write/approval paths in P2.
- All tools in P1: rejected; shell sandbox is a phase-sized problem and would
  delay the kernel validation.