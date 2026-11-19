# ADR 0002: Event-sourced sessions with SQLite as the source of truth

- Status: Accepted
- Date: 2026-11-19

## Context

The harness must support tracing, recovery, UI rendering and usage accounting.
A mutable "messages only" store loses the facts needed to explain a run and to
resume after a crash. Pure in-memory state is not durable.

## Decision

Persist every fact event with a per-session monotonic `seq`. Messages,
tool-call rows and events are written in explicit transactions. SQLite (WAL) is
the durable source of truth; large payloads will move to an artifact store in
P3. State is derived from rows plus events; side effects are never replayed.

## Consequences

- A complete audit trail and deterministic UI/trace replay.
- Recovery can distinguish completed, failed, cancelled and unknown tool calls.
- Events add write volume; streaming deltas are therefore ephemeral and only
  final messages/facts are persisted.
- Schema migrations are a first-class concern.

## Alternatives

- JSONL transcript only: simple, but weak querying and transactional invariants.
- ORM: rejected; explicit SQL keeps the fencing/transaction behavior auditable.
- Event-only store with no message table: rejected because the runtime needs a
  fast ordered transcript and message pairing checks.