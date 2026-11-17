# Ledger guarantees

The schema, the invariants PostgreSQL enforces, how a posting runs, idempotency, the audit trail and statements. Back to the [README](../../README.md).

## Schema and invariants

[`0001_initial_schema`](../migrations/versions/0001_initial_schema.py) creates the schema and
seeds one ledger per supported currency (GBP, EUR), each with its external settlement account.
[`0002_seal_transactions_with_entry_count`](../migrations/versions/0002_seal_transactions_with_entry_count.py)
seals every transaction with its declared number of entries.
[`0003`](../migrations/versions/0003_forbid_truncating_ledger_tables.py) forbids TRUNCATE on the
ledger tables, [`0004`](../migrations/versions/0004_add_audit_events.py) adds the audit trail and
[`0005`](../migrations/versions/0005_order_ledger_entries.py) gives entries a commit-ordered
sequence number, [`0006`](../migrations/versions/0006_enforce_entry_sequence_order.py) rejects
entries numbered out of order, and [`0007`](../migrations/versions/0007_read_entry_sequence_as_definer.py)
lets that check work for a least-privilege role (a narrow `SECURITY DEFINER` sequence read);
[`0008`](../migrations/versions/0008_pin_entry_sequence_trigger_search_path.py) and
[`0009`](../migrations/versions/0009_pin_transaction_balance_check_search_path.py) pin the
`search_path` of the ordering and double-entry checks so temporary tables cannot shadow the
tables they read; [`0010`](../migrations/versions/0010_add_idempotency_keys.py) adds the
append-only idempotency keys and
[`0011`](../migrations/versions/0011_link_transactions_to_idempotency_keys.py) makes each keyed
transaction and its key reference each other, and
[`0012`](../migrations/versions/0012_add_authentication.py) adds password credentials, bearer-token
sessions and the authentication audit actions.

```
users 1 ── 0..* accounts *── 1 ledgers 1 ── * transactions ── 0..1 audit_events (succeeded)
  │               │                              │  │
  │               └──── * ledger_entries * ──────┘  └── 0..1 idempotency_keys
  ├── 0..* audit_events (actor)                                   │
  └── 0..* idempotency_keys ──────────────────────────────────────┘
```

| Table | Purpose |
|---|---|
| `users` | Identity only (lowercase, unique email) |
| `ledgers` | One per currency: the boundary money cannot cross |
| `accounts` | `customer` (owned by a user, one per currency) or `system` (settlement, one per ledger). `balance_minor` is a cached projection of the entries |
| `transactions` | A posted transaction: `deposit`, `withdrawal` or `transfer`, sealed with its `entry_count`; a keyed posting also names its idempotency key. Append-only |
| `ledger_entries` | One signed `amount_minor` on one account, with a `sequence_number` that orders each account's entries in commit order. Append-only |
| `audit_events` | One row per audited attempt: action, outcome (`succeeded`, `rejected`, `failed`), reason code, idempotency key, safe JSON details. Append-only |
| `idempotency_keys` | One row per key a user consumed with a **committed** posting: the request fingerprint and the transaction it produced. Append-only, kept indefinitely |
| `user_credentials` | A user's Argon2id password hash (only that encoding is accepted). Optional: users created before migration 0012 have none and cannot log in |
| `auth_sessions` | One row per issued bearer token: only its SHA-256 digest, the database-time `created_at` and `expires_at`, and `revoked_at`. Rows are never edited except to be revoked, once |

Conventions: UUIDv7 primary keys (exception: `audit_events.id` is a `BIGINT` identity, never
exposed; its values have gaps); money is `BIGINT` minor units named `*_minor`; timestamps are
`timestamptz`; every foreign key is `ON DELETE RESTRICT`; constraint names follow a fixed
convention (`ck_`, `uq_`, `fk_`, `ix_`, `pk_`).

Invariants enforced by PostgreSQL itself, so every code path is bound by them:

| Invariant | Mechanism |
|---|---|
| Every transaction has at least 2 entries summing to 0 | Deferred constraint triggers, checked at `COMMIT` |
| A posted transaction can never gain entries, even balanced ones | `entry_count` in the immutable header, checked by the same trigger |
| Posted transactions, entries and audit events are never updated, deleted or truncated | `BEFORE UPDATE OR DELETE` (row) and `BEFORE TRUNCATE` (statement) triggers |
| An entry, its transaction and its account share one ledger (no cross-currency postings) | Composite foreign keys on `(…, ledger_id)` |
| Customer balances never go negative; settlement balances may | `CHECK (kind = 'system' OR balance_minor >= 0)` |
| One account per user per currency, one settlement account per ledger | Unique constraint, partial unique index |
| An account appears at most once per transaction | Unique `(transaction_id, account_id)` |
| An account's history is a total order, and can only be appended to | Unique `(account_id, sequence_number)`, `GENERATED ALWAYS AS IDENTITY (CACHE 1)`, and a `BEFORE INSERT` trigger rejecting a number not after the account's latest or never issued by the sequence. `GENERATED ALWAYS` alone is not enough: `OVERRIDING SYSTEM VALUE` bypasses it |
| An audit event is well-formed: reason exactly when not succeeded, a transaction exactly for successful postings (at most one event per transaction), an actor for every success, details a JSON object of at most 2048 bytes | CHECK and unique constraints on `audit_events` |
| Audit evidence is not dropped by a routine downgrade | Migration 0004 refuses to drop a non-empty `audit_events` |
| A key is used once per user, by exactly one committed transaction, and never changes | Primary key `(user_id, key)`, unique `transaction_id` with a foreign key checked at `COMMIT`, append-only triggers; 0010's downgrade refuses a non-empty table |
| A key is 1–255 visible ASCII characters; a fingerprint is 32 bytes | CHECK constraints |
| A key and its transaction reference each other, and the transaction is the key owner's own posting: no key can be attached to an existing or another user's transaction | Columns on the immutable `transactions` row, unique and foreign-key constraints, and a link check at `COMMIT` on both tables (migration 0011) |
| No plaintext password or token is stored; a session lasts at most 24 hours and can only be revoked once | An Argon2id-only `CHECK` on `user_credentials`; a unique 32-byte digest, an expiry-window `CHECK` and a revoke-only trigger on `auth_sessions` (migration 0012) |

Triggers bind ordinary sessions only: a superuser or the table owner can disable them
([ADR 0004](adr/0004-immutable-transactions-without-status.md)). Since Phase 10 the API is
neither: it connects as `ledger_app`, which owns nothing and holds only migration 0013's
privileges, so these rules hold against the application itself, not only against its bugs
([Database roles](security.md#database-roles)).

`balance_minor` is maintained by the application inside the posting transaction, not by a
constraint. Reconciliation queries ([`data/reconciliation.py`](../src/ledger_api/data/reconciliation.py))
recompute every invariant from the raw rows, without trusting the triggers and foreign keys
that normally enforce it ([ADR 0012](adr/0012-statement-ordering-and-reconciliation.md)):
every balance equals its entries; every ledger's entries and balances sum to zero (so the
settlement account mirrors the customers' money); every transaction has at least 2 entries
summing to zero and exactly its declared count; every entry sits in its account's and
transaction's ledger; every posted transaction has its succeeded audit event; and failed
events whose transaction committed after all (a lost acknowledgement) are reported, as are
audit events whose attempted transaction id is malformed (which would otherwise hide them).

## Posting

Deposits, withdrawals and transfers ([`services/posting.py`](../src/ledger_api/services/posting.py))
each run in one database transaction ([ADR 0010](adr/0010-posting-protocol.md)):

1. Validate the request (amount, distinct accounts) before touching the database.
2. Lock every account involved in **one** statement, in ascending id order
   (`ORDER BY id FOR UPDATE OF accounts`): all postings take locks in the same order, so they
   cannot deadlock, and no ledger row is ever locked.
3. Check ownership, currency and funds against the locked rows only.
4. Write the transaction, its entries and relative balance updates, then the succeeded audit
   event; `COMMIT` runs the deferred checks. Any failure rolls back everything, the audit
   event included.

Deadlocks and serialization failures retry the whole transaction (3 attempts at most); lock
or statement timeouts and exhausted retries report `temporarily_unavailable`. Amounts are
limited to 100,000,000 minor units per transaction. A keyed posting first claims its
idempotency key, right after `BEGIN` and before any lock (next section).

## Idempotency

Every money-moving request carries an `Idempotency-Key`
([ADR 0006](adr/0006-idempotency-inside-the-posting-transaction.md)). PostgreSQL is the
only source of truth; Redis is never involved.

- **The claim is part of the posting.** `INSERT ... ON CONFLICT (user_id, key) DO NOTHING` is
  the first write of the posting transaction, so the key commits with the posting or not at
  all. A rejected or failed request leaves no key: its retry with the same key runs normally.
- **Same key, same request:** the original `201` body is returned again with
  `Idempotent-Replayed: true`, and nothing is written or audited. The body is rebuilt from the
  ledger: `balance_after_minor` is the balance right after the original entry (the statements'
  running-balance calculation), never today's balance.
- **Same key, different request:** `422 idempotency_key_mismatch`, audited as rejected. The
  comparison uses a SHA-256 fingerprint of the business fields: operation, user, source
  account, destination (transfers) and amount. Formatting differences never count.
- **Keys are scoped per user.** The same key from two users means two requests.
- **Concurrent duplicates** wait on the first request's uncommitted key (up to the 2 s
  `lock_timeout`), then replay it, or run if it rolled back. Past the timeout they get `503
  temporarily_unavailable` and can retry. Keys are claimed before accounts are locked, so they
  add no deadlocks.
- **Unknown outcome.** If the connection drops during `COMMIT`, the API answers `503
  outcome_unknown`. It never claims the posting failed. Retrying with the same key consults the
  durable key row through the claim itself. If the original is still finishing its `COMMIT`,
  its row is invisible to a plain `SELECT`, but the claim waits for it, so the retry learns the
  real result: a replay, or a fresh, single execution.
- **Retention:** indefinite (MVP). Rows cannot be updated, deleted or truncated.

## Audit trail

User registration, account opening, every posting attempt, logins (successful and rejected)
and logouts are recorded in `audit_events`
([ADR 0011](adr/0011-audit-trail.md), [ADR 0013](adr/0013-authentication-and-authorization.md)).
A rejected login records the email's user when the email is registered, never the email itself.
Every event records the `request_id` of the HTTP request it happened in (NULL outside one).
Requests refused with `401`, `403` or `429` are not audited (no service runs):

- **Succeeded** events are the last write of the business transaction: they commit with it or
  not at all, even when a deferred check rejects the transaction at `COMMIT`. If the audit
  write fails, the operation fails.
- **Rejected** (a business rule, with the error code as `reason`) and **failed** (contention
  or an unexpected error) events are written *after* the business transaction has rolled back
  and released its connection, in a new transaction on the same session. One operation never
  holds two connections.
- The audit writer never raises (except to let a cancellation through). If it cannot write, it
  logs an ERROR with the event (the log is the fallback sink), and the caller still gets its
  original error. Rejected and failed events are therefore best-effort. A fallback line is not
  an audit record; see [Audit events and fallback log lines](failure-modes.md#audit-events-and-fallback-log-lines)
  for what each line does and does not prove.
- A failure to obtain a database connection is recorded with reason `temporarily_unavailable`,
  the code the caller receives, when the event can still be written.
- Details hold ids, amounts, currencies and error identifiers only: never an email address, an
  exception message or a request body. A claimed user that does not exist is stored as a NULL
  actor, with its id in `details.requested_user_id`.
- Reads, health checks and malformed requests are not audited.

Under connection-pool exhaustion the post-rollback audit write waits for a connection (up to
the pool timeout) before giving up, which delays the error response. This is accepted for now.

## Statements

`services.statements.get_statement` returns an owner's account history, newest first, with the
balance after each entry ([ADR 0012](adr/0012-statement-ordering-and-reconciliation.md)),
served by `GET /users/{user_id}/accounts/{account_id}/statement`.
`get_account_transaction` serves a single transaction in the same entry shape, with the same
historical balance calculation (also used by idempotent replays).

- Entries are ordered by `sequence_number`. It is drawn while the posting holds the account's
  row lock, which it keeps until `COMMIT`, so for one account sequence order is commit order.
  `created_at` is not used: it is the transaction's start time. Entries written before
  migration 0005 were numbered in UUIDv7 id order (best-effort history).
- Running balances are computed by the query, never stored. A page, its running balances and
  the current balance come from one SQL statement (one snapshot).
- Keyset pagination with the last entry id as cursor, 1–100 entries per page. Postings made
  between page requests only appear on a fresh first page; following the cursor never repeats
  or skips an entry.
- No counterparty is shown. If the balance does not equal the sum of the entries, the service
  refuses (`internal_error`) and logs an ERROR instead of returning a possibly wrong statement.
- **Cost (known limitation):** running balances are recomputed on every read. Each statement
  page scans the account's whole history, and a single-transaction read sums the account's
  entries up to that one, so both grow linearly with the account's history. Acceptable for the
  MVP; the documented upgrade path is persisted running balances with a gapless per-account
  sequence ([ADR 0012](adr/0012-statement-ordering-and-reconciliation.md), "Alternatives
  considered").
