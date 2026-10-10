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

## Amendment (Phase 6, 2026-10-10)

Implemented in migration 0010, `domain/idempotency.py`, `data/idempotency.py` and
`services/posting.py`, with these refinements:

- **Endpoints.** The keyed operations are `POST /users/{user_id}/accounts/{account_id}/deposits`,
  `/withdrawals` and `/transfers` (ADR 0009's owner scope), not one `POST /transactions`.
- **The header.** `Idempotency-Key` is required: missing is `428 idempotency_key_required`;
  malformed or sent twice is `422 invalid_idempotency_key`. A key is 1–255 **visible ASCII**
  characters (0x21–0x7E), checked by the API, the service and a `CHECK` constraint. Neither
  case reaches a service, so neither is audited, and the value is never echoed. The service
  parameter is optional, for internal callers; a malformed one there is a `ValueError`.
- **The fingerprint replaces "method, path and canonical body".** It is SHA-256 over a
  versioned canonical JSON of the **business fields**: operation, `user_id`, the caller's
  (source) account, the destination (transfers only, `null` otherwise) and `amount_minor`.
  It is built from validated values, so JSON spelling (whitespace, key order) cannot make a
  retry look different, and every field that changes the posting's meaning is covered. It is
  computed **after** pre-validation (amount, distinct accounts), so an invalid request is
  rejected for what it is, even with a key that is already in use. The same key with a
  different fingerprint is `422 idempotency_key_mismatch`.
- **The claim** is `INSERT ... ON CONFLICT (user_id, key) DO NOTHING RETURNING`, the first
  statement after `BEGIN`, before the settlement lookup and every account lock. A duplicate never
  raises `23505`, so the transaction is never left aborted. If no row comes back, a second
  statement (a fresh READ COMMITTED snapshot) reads the committed row the conflict was with.
  The global lock order becomes "key, then accounts". A request waiting on a key holds no account
  lock, so keys add no deadlock cycle; the concurrency tests run with retries disabled.
- **What is stored.** `idempotency_keys(user_id, key, request_fingerprint, transaction_id,
  created_at)`. The transaction id is generated before the header exists (UUIDv7 in Python), so
  its foreign key is `DEFERRABLE INITIALLY DEFERRED`: a committed key always references a
  committed transaction, and each transaction has at most one key.
- **Replays are rebuilt, not stored.** No response snapshot is kept. A replay reads the stored
  transaction and the caller's entry, and computes the balance right after that entry as the
  sum of the account's entries up to it in `sequence_number` order, the statements' calculation
  (ADR 0012). `accounts.balance_minor` is never used, so a replay after later postings still
  shows the original balance. The response is `201` with the original body and
  `Idempotent-Replayed: true`. Replays write nothing and are not audited.
- **Only committed postings own a key.** A rejected or failed request rolls its claim back with
  everything else, so its retry with the same key runs normally, and may succeed. This is
  deliberate: a rejection means nothing happened, and caching it would need a write outside the
  posting transaction.
- **Unknown COMMIT outcome.** If COMMIT fails without PostgreSQL reporting why (a lost
  connection, a driver or network error), the posting may have been applied. The service
  responds `503 outcome_unknown` and tells the client to retry with the **same** key. It never
  says the request failed, and never retries by itself. Errors PostgreSQL returns for a COMMIT
  (SQLSTATE classes 23 and 40, lock and statement timeouts) are definite rollbacks and keep
  their handling. Recovery consults the durable record through the claim, never through a plain
  `SELECT`. While the original backend is still inside its COMMIT, its row is invisible: a
  `SELECT` would find nothing and wrongly conclude "not posted". The retry's `INSERT` instead
  waits for that transaction to end, then replays it, or runs if that COMMIT failed. The wait is
  bounded by `lock_timeout` (2 s). Beyond it the retry gets `503 temporarily_unavailable` and
  can retry again.
- **Unknown users.** Before Phase 8 the URL names the user. A key for a user that does not
  exist violates `fk_idempotency_keys_user_id_users`, which is translated (by exact name) to
  `404 account_not_found`, as for any account that is not the caller's (ADR 0009).
- **Retention.** Keys are kept indefinitely (MVP). The table is append-only like the ledger:
  `UPDATE`, `DELETE` and `TRUNCATE` raise `idempotency_keys_immutable`, and the downgrade of
  0010 refuses to drop a non-empty table. A deleted key would let a retried request run twice.
  The production concern above (unbounded growth, and retention as a security decision) stands.
- **Least privilege.** A posting role needs `SELECT, INSERT` on `idempotency_keys` and nothing
  more (ADR 0012's contract, asserted by tests).
- **Audit.** Every audited posting event carries the key in `audit_events.idempotency_key`
  (ADR 0011).
