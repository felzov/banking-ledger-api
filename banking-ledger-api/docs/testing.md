# Testing

How the suite runs and what each group of tests proves. Back to the [README](../../README.md).

Tests run against real PostgreSQL, never SQLite, because the invariants ([Ledger guarantees](ledger-guarantees.md#schema-and-invariants)) live in
triggers and constraints. They run on their **own disposable cluster**, never the development
database: `make test` starts the `test-db` Compose service (data on tmpfs, `127.0.0.1:5433`),
marks it, and points `DATABASE_URL` at it. The suite creates `<db>_test` databases there,
builds them with the Alembic migrations, and wraps each test in a transaction that is rolled
back.

```bash
make test
```

Safety guards, all required before the suite creates a database or a role (roles are
cluster-wide):

- the environment declares a disposable cluster (`LEDGER_TEST_CLUSTER=disposable`, set by
  `make test` and CI);
- the server `DATABASE_URL` reaches carries the marker `ledger.test_cluster = 'disposable'` in
  its own configuration, set by `make test` and CI on the throwaway server itself, never through
  `DATABASE_URL` (a development server never has it);
- the database it connects to is not migrated;
- and a database is only ever dropped if its name ends in `_test`.

Running `uv run pytest` directly therefore needs a marked, disposable server and both settings;
use `make test`. It also starts `test-redis` and sets `TEST_REDIS_URL` for the rate-limit tests;
each of them counts under a key namespace of its own, deleted afterwards, and the suite fails
rather than skips them without it.

Negative tests assert the exact constraint or trigger that rejected the data, not just that
some error occurred. API tests run each request in a fresh session on the test's rolled-back
connection, so services still open and commit their own transactions. An architecture test
fails if an account route is ever mounted outside `/users/{user_id}`.

Posting is tested at every level:

- **Failure injection** with real commits: a posting broken after its header, after its
  entries, midway through the balance updates, or at `COMMIT` leaves no trace.
- **Concurrency** (`-m concurrency`), on real connections with **retries disabled** so a
  deadlock cannot be retried away: racing withdrawals never overdraw, opposite transfers
  never deadlock, random concurrent transfers conserve money, and a mixed workload of
  deposits, withdrawals and transfers sharing the settlement account ends with exact balances
  and one audit event per posting.
- **Property-based** ([Hypothesis](https://hypothesis.readthedocs.io/)): posting rules hold for
  generated inputs, and random sequences of postings match a pure-Python model step by step.
- **Reconciliation** after every posting test: balances equal entries, ledgers sum to zero,
  transactions and entries are valid. Each reconciliation check also has a test that plants
  the inconsistency it must find, inside a rolled-back transaction (using
  `session_replication_role = replica` where the schema itself would refuse it).

Audit and statement tests:

- **Audit protocol** with real commits: insufficient funds leaves the ledger untouched and
  exactly one rejected event; a failure after the entries, or a deferred-trigger failure at
  `COMMIT`, leaves no succeeded event and one failed event; a failing audit write preserves
  the original exception and logs an ERROR without secrets; a failing success write rolls the
  posting back.
- **One connection per operation**: success, rejection and failures run on a pool of exactly
  one connection, so needing a second one would time out.
- **Concurrency**: 10 racing withdrawals produce 3 succeeded and 7 rejected events with
  consistent balances. A two-session test shows `sequence_number` following lock (commit)
  order while `created_at` follows transaction start.
- **Append-only**: UPDATE, DELETE and TRUNCATE are rejected on every append-only table; a test
  documents that a superuser can bypass the triggers.
- **Migrations**: up/down/up on an empty database; the downgrade refuses to drop a non-empty
  `audit_events`; the sequence backfill on its own database.

Idempotency tests:

- **Duplicates and conflicts** with real commits: replays return the original result and
  write nothing, replayed balances are historical even after later postings, and every changed
  field (amount, operation, account, destination) is a mismatch. Rejected requests leave no key,
  and their retry runs.
- **Rollback**: a failure after the entries, a deferred-trigger failure at `COMMIT`, a failed
  success audit and a deadlock retry each leave no stray key, and the retry posts once.
- **Unknown `COMMIT` outcomes**, checked against the persisted state: a lost acknowledgement
  (committed: the retry replays it, and reconciliation reports the failed/committed pair) and a
  backend terminated before `COMMIT` through the real driver (not committed: the retry runs
  once). A test-only deferred trigger keeps a `COMMIT` in progress server-side, showing that a
  plain `SELECT` sees no key yet while the retry's claim waits, then replays, or runs if that
  `COMMIT` fails.
- **Concurrency** (retries disabled): 10 identical requests post once and replay 9 times;
  mixed requests under one key post one; a waiter runs when the holder rolls back, and gets
  `503` (then a replay) when the holder outlives `lock_timeout`; keyed opposite transfers never
  deadlock.
- **Least privilege**: a role with `SELECT, INSERT` on `idempotency_keys` claims, posts and
  replays; it cannot update, delete or truncate keys; without the grant, a keyed posting fails
  loudly (`42501`).
- **API contract**: `428`/`422` for the header, strict integer amounts, every error code, and the
  OpenAPI schema.

Read API tests (Phase 7):

- **Session boundaries**: every read service returns the session outside a transaction and its
  connection to the pool (on a one-connection pool), including on failure; a posting, and a
  rejected posting with its audit event, work on the same session right after any read.
- **Statement API**: cursor pagination through the whole history, signed amounts and running
  balances, empty accounts, limit bounds and malformed input, unknown and foreign cursors, and
  identical 404s for missing, foreign and system accounts.
- **Transaction API and `Location`**: each kind of posting read back from the owner's account,
  a transfer from the payee's, the historical balance after later postings, identical 404s,
  and a `Location` on every `201` (the same on a replay) that leads to the posted transaction.
- **OpenAPI**: every real `422` body matches the schema its route documents, for both shapes.

Authentication tests (Phase 8):

- **Domain**: the password policy (NFKC, lengths), Argon2id hashing that fails closed on any
  other hash format, and token entropy, format and digests; no secret in `repr` or logs.
- **Schema**: the credential and session constraints, the revoke-only trigger, and the
  migration's upgrade, downgrade refusal (credentials or authentication audit events) and round
  trip.
- **Login and logout** with real commits: only the token digest is stored, with a 30-minute
  expiry in database time; an unknown email and a wrong password fail identically and cost one
  Argon2id verification each; concurrent duplicate registrations create one user; concurrent
  logouts revoke once. On a one-connection pool, Argon2id never runs on the event loop or while
  a database connection is held.
- **Enforcement on every route**: an explicit route inventory; on all nine protected routes,
  missing, malformed, unknown, expired and revoked tokens are `401`, the owner succeeds, and
  another user's token is `403`; refused cross-user postings change no balance, entry,
  idempotency key or audit event; another user cannot replay a keyed posting; no token or digest
  appears in responses, logs or audit details. A full authenticated flow on a one-connection
  pool leaves no transaction open and no connection held after any request.

Reliability tests (Phase 9):

- **Unreachable database and exhausted pool**: connect failures are told apart from unrelated
  `OSError`s and from refusals the server itself reports (an unknown database stays `500`);
  login, an authenticated route and a posting answer `503 temporarily_unavailable` with the
  database down; a posting that gets no pooled connection posts nothing; failure reasons are
  recorded as `temporarily_unavailable` when the event can be written, and logged as lost when
  it cannot.
- **Connection lost before `COMMIT`**, through the real driver (a backend terminated after the
  key claim): `503`, no key, no transaction, a failed event, and a same-key retry that posts
  once. A connection lost *during* `COMMIT` stays `outcome_unknown` (the tests above).
- **Cancellation**, with explicit synchronisation points, never sleeps: after the key claim, and
  while waiting for a row lock, nothing is committed and no transaction or connection is left
  behind; after PostgreSQL has committed (a TCP proxy, [`tests/tcp_proxy.py`](../tests/tcp_proxy.py),
  withholds the `COMMIT` acknowledgement), the posting and its succeeded event stand, one
  WARNING names the transaction, and a same-key retry replays it; a replay cancelled during its
  `COMMIT` logs no warning; a cancelled rejection-audit write is logged as "not written", or as
  "possibly not written" when its own `COMMIT` was interrupted.

Rate limit and observability tests (Phase 10):

- **Counters** against a real Redis: windows that start at the first hit and are never
  extended, expiry, 50 concurrent hits counted exactly, and an unreachable Redis that fails in
  one short attempt without naming its host.
- **Limits over HTTP**: each limit's `429` and `Retry-After`; malformed attempts count; IPv6 per
  `/64`; registered and unknown emails throttled identically, the right password included, and
  cleared by a success; a throttled login computes no hash and writes no audit event; 40
  concurrent logins for one email against a limit of 3 get exactly 3 password checks; postings
  limited per user across kinds and replays, moving nothing when refused. With Redis down,
  login and registration answer `503` and postings still post, with a WARNING.
- **Correlation**: an `X-Request-ID` on 2xx, 401, 404, 422 and 500 responses (and a client's
  own ignored); audit events record the id of their request; access lines carry it; register,
  login and logout leave no email, password or token in any log line; against a real uvicorn,
  the traceback of an unhandled error carries the request's id.
- **Migration 0014**: the privilege matrix gains `audit_events.request_id` only, and the app
  role may insert it.

CI runs the same suite against a PostgreSQL service container (marked disposable) and a Redis
service container, and runs
the start-up smoke test (`scripts/compose-smoke.sh`, `make smoke` locally) on its own Compose
stack: `db` → `bootstrap` → `migrate` (`ledger_owner`) → `api` (`ledger_app`, with Redis), an API
flow with request ids, the API container's environment, and the server's view of who is
connected and who owns the schema.
