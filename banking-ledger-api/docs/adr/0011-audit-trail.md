# ADR 0011: Audit trail

- Status: Accepted
- Date: 2026-10-10
- Refines: [ADR 0007](0007-auditing-rejected-financial-attempts.md)

## Context

ADR 0007 decided *when* audit events are written: successes inside the business transaction,
rejections and failures after it rolls back. Phase 5 implements it. This ADR records the
schema, the exact write protocol, what is audited, and what the protection does and does not
guarantee.

## Decision

### What is audited

| Action | Audited outcomes |
|---|---|
| `user.register` | succeeded; rejected (`email_already_registered`); failed |
| `account.open` | succeeded; rejected (`account_already_exists`, `user_not_found`); failed |
| `posting.deposit`, `posting.withdrawal`, `posting.transfer` | succeeded; rejected (`account_not_found`, `destination_account_not_found`, `currency_mismatch`, `insufficient_funds`, `invalid_amount`, `same_account_transfer`); failed (`temporarily_unavailable`, `internal_error`) |

Not audited, logs only: reads (users, accounts, statements), health checks, malformed HTTP
bodies (422 before any service runs), and cancelled requests. There are no unauthenticated
requests to distinguish until Phase 8.

### Schema (`audit_events`, migration 0004)

| Column | Notes |
|---|---|
| `id` | `BIGINT GENERATED ALWAYS AS IDENTITY`. A deliberate exception to the UUIDv7 convention: audit ids are never exposed and never built before I/O. **Ids have gaps** (a rolled-back success event consumes one), so a gap is never evidence of a deleted event |
| `occurred_at` | `now()`: the start of the writing transaction. A success event shares it with its transaction's `created_at` |
| `actor_user_id` | Nullable FK to `users` |
| `action`, `outcome`, `reason` | Stable identifiers. `reason` is a `DomainError` code or `internal_error` |
| `transaction_id` | Nullable FK to `transactions`, unique |
| `request_id`, `idempotency_key` | Reserved, NULL until Phases 10 and 6 |
| `details` | `JSONB` object of safe metadata: ids, amounts, currencies, error type, SQLSTATE, constraint name. Never an email address, an exception message or a request body |

Database-enforced rules: `reason IS NULL` exactly for `succeeded`; `transaction_id` present
exactly for successful postings, and unique (a transaction is audited at most once); a success
always has an actor; `details` is a JSON object of at most **2048 bytes** (its JSON text);
`reason` matches `^[a-z][a-z0-9_]{0,63}$`. `domain.audit.AuditRecord` mirrors every rule before
any I/O and accepts only JSON scalars as detail values.

### Unknown actors

Until Phase 8 a caller can name any user id. `insert_audit_event` resolves the actor in the
same statement (`actor_user_id = (SELECT id FROM users WHERE id = :claimed)`): an unknown user
is stored as NULL and its id kept in `details.requested_user_id`. An unknown id can therefore
never turn an audit write into a foreign-key violation. Users are never deleted, so the
lookup cannot race.

### Write protocol

**Succeeded.** The event is the last write of the business transaction. It commits with the
operation or not at all, including when a deferred trigger rejects the transaction at
`COMMIT`. If the audit insert itself fails, the operation fails: no audit, no money movement.
Each retry attempt writes its own event; only the committed attempt's survives.

**Rejected or failed.** `services.audit.audited()` wraps the service call:

1. The business transaction ends (`async with session.begin()` rolls back) and its connection
   returns to the pool.
2. `record_outside_transaction()` writes the event in a **new transaction on the same
   session**. One operation never holds two connections. The writer refuses, and logs, if the
   session is somehow still in a transaction.
3. The original exception is re-raised unchanged (bare `raise`).

The writer **never raises**. If the insert fails, it logs an ERROR carrying the event's fields
(the log is the fallback sink) and the caller's error stands. Building the record cannot
become a second error either: a failure there is logged and skipped.

Classification: `DomainError` → rejected, except `UnavailableError` and `InternalError`, which
are failed. Any other `Exception` → failed/`internal_error`. Failed events carry
`error_type`, `sqlstate`, `constraint` and, for postings whose header was flushed,
`attempted_transaction_id`; `connection_invalidated` marks a lost connection.

### Append-only protection, and its limits

`BEFORE UPDATE OR DELETE` (row) and `BEFORE TRUNCATE` (statement) triggers raise
`audit_events_immutable`. The downgrade of migration 0004 **refuses to drop a non-empty
`audit_events` table**: audit evidence is rolled forward, never discarded by a routine
downgrade. An empty database still round-trips.

**The application currently connects as a PostgreSQL superuser** (the Compose bootstrap
role). Triggers bind ordinary sessions only: a superuser or the table owner can disable or drop
them, or skip them with `session_replication_role = replica` (a test demonstrates this). The
append-only guarantee therefore protects against application bugs and mistakes, **not against
that role**. Least-privilege roles (an owner role for migrations; an application role with
`INSERT`/`SELECT` only on the append-only tables, no `TRUNCATE`, not the owner) are recorded as
future hardening work for Phase 10 and are not part of Phase 5.

## Alternatives considered

- **UUIDv7 primary key**: consistent with the other tables, but it buys nothing here and costs
  16 bytes per row; nothing outside the database refers to an audit event.
- **A foreign key on the claimed actor without resolution**: every rejection for an unknown
  user would fail its own audit write.
- **No foreign key on the actor**: survives user deletion (not possible today), but loses the
  guarantee that a stored actor is real. Revisit with a GDPR erasure design.
- **A hash chain for tamper evidence**: serializes every audit write on one row and still does
  not stop a superuser from rewriting the chain. Out of scope.
- **A second session from a sessionmaker for the audit write**: needs a sessionmaker threaded
  through every service, for no gain once the first connection has been released.

## Consequences

- Successful operations are audited exactly once, atomically.
- Rejected and failed attempts are audited **best-effort**: a crash between the rollback and the
  audit commit loses the row (the ADR 0007 trade-off).
- **Pool exhaustion:** the rejection path checks a connection out again. Under exhaustion it
  waits up to the pool timeout (30 s by default) before the writer gives up and logs, which
  delays the error response. No `asyncio.timeout` is added in Phase 5; the trade-off is
  accepted and documented.
- **Commit outcome ambiguity:** if the connection drops during `COMMIT`, the posting may have
  committed (with its succeeded event) while the caller records a failed event.
  `find_failed_events_for_committed_transactions` reports these; idempotency keys (Phase 6) let
  the client retry safely.
- Unauthenticated callers can grow the table with rejected attempts. Rate limiting (Phase 10,
  ADR 0008) and a retention policy are future work.
- ERROR logs currently reach stderr through Python's last-resort handler; structured logging
  is Phase 10.
