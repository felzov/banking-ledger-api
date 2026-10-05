# ADR 0006: Idempotency keys stored inside the posting transaction

- Status: Accepted
- Date: 2026-10-05

## Context

Clients retry after timeouts without knowing whether the first request committed. A retry
must not move money twice.

## Decision

- `POST /transactions` requires an `Idempotency-Key` header. If it is missing, the API
  responds 428.
- The key row is inserted **in the same database transaction** as the posting, as its first
  step. `UNIQUE (user_id, key)` makes a concurrent duplicate block until the first
  transaction ends. Then:
  - **The first one committed**: replay the stored response with the header
    `Idempotent-Replayed: true`.
  - **The first one rolled back**: the key never existed, so the retry runs normally. This is
    safe because nothing happened.
- The stored request hash covers method, path and canonical body. The same key with a
  different hash returns 422.
- Keys are scoped per user, so one user cannot probe or replay another user's keys.
- Keys are kept indefinitely in the MVP.

## Alternatives considered

- **Redis as the idempotency store**: a crash between the Redis write and the database commit
  breaks exactly-once behavior. Rejected. Redis is never used for idempotency.
- **Check, then insert, without a unique constraint**: racy.

## Consequences

- Exactly-once effects without a distributed lock.
- The approach is valid only while the transaction performs no external I/O. With real
  payment rails it would need a recovery-point state machine and an outbox.
- **Production concern:** keeping keys forever means unbounded growth. A retention policy is
  needed (for example 30–90 days with a cleanup job). Once a key is purged, a captured old
  request could execute again, so retention length is a security decision, not just storage
  housekeeping.
