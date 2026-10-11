# banking-ledger-api

A production-oriented banking backend built around a **double-entry ledger**: ACID money
movement, concurrency control, idempotent APIs, and an append-only audit trail.

> **Status: Phase 7 of 10. The REST API: postings, statements and transactions.**
> Users can be registered and customer accounts opened and read. Deposits, withdrawals and
> transfers are exposed over HTTP, each requiring an `Idempotency-Key`: a retried request never
> moves money twice, and an unknown COMMIT outcome is recoverable. An account's statement (its
> history with running balances) and any single transaction can be read back, always from the
> owner's side. Every attempt is recorded in an append-only audit trail. **There is no
> authentication yet (Phase 8).** The full design is documented in the [ADRs](docs/adr/).

> [!WARNING]
> **The API is unauthenticated until Phase 8.** Anyone who knows a user ID can act as that
> user: move that user's money, replay their keyed requests, and read their accounts,
> statements and transactions. Ownership scoping (below) only guarantees that an account is
> reached through its true owner, not who the caller is. Do not expose the API to untrusted
> clients. See [ADR 0009](docs/adr/0009-owner-scoped-access-and-error-responses.md).
>
> **The application connects to PostgreSQL as a superuser** (the Compose bootstrap role). The
> append-only triggers protect against application bugs, not against that role, which can
> disable them. Least-privilege database roles are planned hardening (Phase 10).

All data is synthetic. No real financial information, credentials or personal data are used.

## Technology stack

| Concern | Choice |
|---|---|
| Language | Python 3.14 |
| Web framework | FastAPI, Pydantic v2 |
| Database | PostgreSQL 18, SQLAlchemy 2.1 (async, asyncpg driver), Alembic migrations |
| Tooling | uv, ruff, mypy (strict), pytest, pre-commit (including gitleaks) |
| Runtime | Docker, Docker Compose |

## Local setup

Prerequisites: Docker with Compose v2+, [uv](https://docs.astral.sh/uv/), and GNU Make.

```bash
cd banking-ledger-api
cp .env.example .env        # synthetic local-only values
make install                # create .venv from uv.lock
make up                     # start PostgreSQL, apply migrations, start the API
curl localhost:8000/health/ready
```

`make up` starts three services in order: `db`, then a one-shot `migrate` container
(`alembic upgrade head`), then `api` once the migrations have succeeded.

Interactive API docs: <http://localhost:8000/docs>

To enable the git hooks, run this once from the repository root:

```bash
banking-ledger-api/.venv/bin/pre-commit install
```

## Environment variables

| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `APP_ENV` | api | `development` | One of `development`, `test`, `production`. Validated at startup |
| `DATABASE_URL` | api, alembic, tests | (required) | `postgresql+asyncpg://…`. Validated at startup. Not set in `.env`: Compose and the Makefile derive it from the `POSTGRES_*` values |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | db | (required) | Database bootstrap credentials |
| `POSTGRES_PORT` | compose | `5432` | Host port, bound to 127.0.0.1 |
| `API_PORT` | compose | `8000` | Host port, bound to 127.0.0.1 |

## Development commands

| Command | Action |
|---|---|
| `make up` / `make down` | Start or stop the stack. Data is kept in the `pgdata` volume |
| `make logs` | Follow container logs |
| `make migrate` | Apply migrations from the host (the stack must be up) |
| `make revision m="..."` | Create a new, empty migration |
| `make db-shell` | Open `psql` in the database container |
| `make test` | Run the test suite (needs PostgreSQL: run `make up` first) |
| `make lint` / `make fmt` | Lint and format check, or auto-fix |
| `make typecheck` | `mypy --strict` |
| `make check` | All quality gates |

## API

| Method | Path | Description |
|---|---|---|
| GET | `/health/live` | Liveness: the process is serving requests. It checks no dependencies |
| GET | `/health/ready` | Readiness: PostgreSQL answers `SELECT 1` within 2 s. Otherwise `503 {"status": "unavailable"}` |
| POST | `/users` | Register a user: `{"email"}`. `201` + `Location`; `409` if the email exists |
| GET | `/users/{user_id}` | Get a user |
| POST | `/users/{user_id}/accounts` | Open a customer account: `{"currency": "GBP" \| "EUR"}`. Balance starts at 0. `409` if the user already has one in that currency |
| GET | `/users/{user_id}/accounts` | List the user's accounts: `{"items": [...]}` |
| GET | `/users/{user_id}/accounts/{account_id}` | Get one of the user's accounts |
| GET | `/users/{user_id}/accounts/{account_id}/statement?limit=&cursor=` | The account's history, newest first, with the balance after each entry. Keyset pagination: `limit` 1–100 (default 50), `cursor` = the previous page's `next_cursor` |
| GET | `/users/{user_id}/accounts/{account_id}/transactions/{transaction_id}` | One transaction as it appears on this account (the same shape as a statement entry) |
| POST | `/users/{user_id}/accounts/{account_id}/deposits` | Money in from outside: `{"amount_minor"}`. Requires `Idempotency-Key`. `201` + `Location` |
| POST | `/users/{user_id}/accounts/{account_id}/withdrawals` | Money out to outside: `{"amount_minor"}`. Requires `Idempotency-Key`. `201` + `Location` |
| POST | `/users/{user_id}/accounts/{account_id}/transfers` | To any customer account in the same currency: `{"destination_account_id", "amount_minor"}`. Requires `Idempotency-Key`. `201` + `Location` |

There is deliberately no separate `/balance` endpoint (`GET .../accounts/{account_id}` returns
`balance_minor`) and no separate transaction list (the statement is the account's history).

Interactive documentation, including every request and response schema:
<http://localhost:8000/docs>.

### Example

```bash
curl -s -X POST localhost:8000/users -H 'content-type: application/json' \
  -d '{"email": "Alice@Example.com"}'
# 201 {"id": "<uuid>", "email": "alice@example.com", "created_at": "..."}

curl -s -X POST localhost:8000/users/<user_id>/accounts -H 'content-type: application/json' \
  -d '{"currency": "GBP"}'
# 201 {"id": "<uuid>", "currency": "GBP", "balance_minor": 0, "created_at": "..."}

curl -si -X POST localhost:8000/users/<user_id>/accounts/<account_id>/deposits \
  -H 'content-type: application/json' -H 'Idempotency-Key: deposit-2026-10-10-001' \
  -d '{"amount_minor": 2500}'
# 201 {"transaction_id": "<uuid>", "kind": "deposit", "account_id": "<uuid>",
#      "amount_minor": 2500, "balance_after_minor": 2500, "created_at": "..."}

# Location: /users/<user_id>/accounts/<account_id>/transactions/<transaction_id>

# The same request again (e.g. after a timeout): nothing is posted twice.
# 201, same body, same Location, plus the header  Idempotent-Replayed: true

curl -s localhost:8000/users/<user_id>/accounts/<account_id>/statement?limit=2
# 200 {"account_id": "<uuid>", "balance_minor": 2500,
#      "entries": [{"entry_id": "<uuid>", "transaction_id": "<uuid>", "kind": "deposit",
#                   "amount_minor": 2500, "balance_after_minor": 2500, "created_at": "..."}],
#      "next_cursor": null}

curl -s localhost:8000/users/<user_id>/accounts/<account_id>/transactions/<transaction_id>
# 200 the same entry as on the statement
```

### Rules

- **Accounts are only reachable through their owner.** Every user-scoped route lives under
  `/users/{user_id}`. Another user's account, a system (settlement) account and a missing
  account all return the same `404`, so responses never reveal which IDs exist.
- **Clients never choose** an account's owner (it comes from the URL), its kind (always
  `customer`) or its balance (starts at 0). Unknown request fields are rejected with `422`.
- **Emails** are stored in one canonical form: trimmed, syntax-checked, lowercased.
  ASCII-only addresses are a deliberate MVP boundary. No provider-specific rules (Gmail dots
  and `+tags` are kept).
- **Amounts** are whole minor units (`2500` is £25.00), JSON integers only: `"2500"`, `25.0`
  and `true` are rejected. Valid amounts are 1–100,000,000 per posting.
- **Responses show only the caller's balance**, never a counterparty's.
- **Statements and transactions are account-relative.** Each entry is the account's own side of
  a transaction: `amount_minor` is signed (negative debits the account, positive credits it),
  and `balance_after_minor` is the account's balance right after that entry, computed from its
  history. It is historical: later postings never change it, and it is not the current
  balance (that is the statement's `balance_minor`). A transfer shows the payer a negative
  entry and the payee a positive one, each with only their own balance. No counterparty is
  ever shown.
- **Pagination** follows `next_cursor` until it is `null`. Postings made between page requests
  only appear on a fresh first page; following the cursor never repeats or skips an entry.
- **Not found, without revealing anything.** For the statement and transaction reads, the
  account is checked first: someone else's, a system or a missing account is
  `404 account_not_found`. Only then is the transaction looked up, among this account's entries
  only: a transaction that does not exist, belongs to other accounts, or is the user's own
  posting on a different account is the same `404 transaction_not_found`. An unknown cursor
  and another account's entry are the same `422 invalid_statement_cursor`.
- **`Location`** on every `201` posting, replays included, names the transaction as it appears
  on the caller's account (for a transfer, the source account). A replay points at the
  original transaction.

### Errors

Every error body is `{"code": "...", "detail": ...}`:

| Status | `code` | When |
|---|---|---|
| 404 | `user_not_found`, `account_not_found` | Unknown user; account missing or not the user's (a posting, statement or transaction read for an unknown user is `account_not_found`) |
| 404 | `destination_account_not_found` | A transfer's destination is missing or not a customer account |
| 404 | `transaction_not_found` | The transaction has no entry on this account (it does not exist, or belongs to other accounts) |
| 409 | `email_already_registered`, `account_already_exists` | Uniqueness rules (enforced by the database) |
| 422 | `validation_error` | Invalid request. `detail` lists `{type, loc, msg}`; submitted values are never echoed |
| 422 | `invalid_amount`, `same_account_transfer`, `currency_mismatch`, `insufficient_funds` | Posting rules |
| 422 | `idempotency_key_mismatch` | The `Idempotency-Key` was already used for a different request |
| 422 | `invalid_statement_limit`, `invalid_statement_cursor` | A `limit` outside 1–100; a cursor that is not an entry of this account. (A non-integer `limit` or a malformed cursor is `validation_error`) |
| 422 | `invalid_idempotency_key` | The header is not one value of 1–255 visible ASCII characters (never echoed) |
| 428 | `idempotency_key_required` | A posting without an `Idempotency-Key` header |
| 503 | `temporarily_unavailable` | Lock or statement timeout, or contention retries exhausted. Nothing was posted |
| 503 | `outcome_unknown` | The connection was lost during COMMIT: the posting may or may not have been applied. **Retry with the same `Idempotency-Key`** |
| 500 | `internal_error` | Anything unexpected, including a statement whose balance does not match its entries (refused rather than returned). No details are returned; the traceback goes to the server log |

Two body shapes share `422`, and the OpenAPI schema documents both where both can occur:
`validation_error` carries a list of `{type, loc, msg}`; every other code carries a string.

## Database

[`0001_initial_schema`](migrations/versions/0001_initial_schema.py) creates the schema and
seeds one ledger per supported currency (GBP, EUR), each with its external settlement account.
[`0002_seal_transactions_with_entry_count`](migrations/versions/0002_seal_transactions_with_entry_count.py)
seals every transaction with its declared number of entries.
[`0003`](migrations/versions/0003_forbid_truncating_ledger_tables.py) forbids TRUNCATE on the
ledger tables, [`0004`](migrations/versions/0004_add_audit_events.py) adds the audit trail and
[`0005`](migrations/versions/0005_order_ledger_entries.py) gives entries a commit-ordered
sequence number, [`0006`](migrations/versions/0006_enforce_entry_sequence_order.py) rejects
entries numbered out of order, and [`0007`](migrations/versions/0007_read_entry_sequence_as_definer.py)
lets that check work for a least-privilege role (a narrow `SECURITY DEFINER` sequence read);
[`0008`](migrations/versions/0008_pin_entry_sequence_trigger_search_path.py) and
[`0009`](migrations/versions/0009_pin_transaction_balance_check_search_path.py) pin the
`search_path` of the ordering and double-entry checks so temporary tables cannot shadow the
tables they read; [`0010`](migrations/versions/0010_add_idempotency_keys.py) adds the
append-only idempotency keys and
[`0011`](migrations/versions/0011_link_transactions_to_idempotency_keys.py) makes each keyed
transaction and its key reference each other.

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

Triggers bind ordinary sessions only. A superuser or the table owner can disable them, and the
application currently connects as a superuser, so these rules guard against application bugs,
not against that role ([ADR 0004](docs/adr/0004-immutable-transactions-without-status.md)).

`balance_minor` is maintained by the application inside the posting transaction, not by a
constraint. Reconciliation queries ([`data/reconciliation.py`](src/ledger_api/data/reconciliation.py))
recompute every invariant from the raw rows, without trusting the triggers and foreign keys
that normally enforce it ([ADR 0012](docs/adr/0012-statement-ordering-and-reconciliation.md)):
every balance equals its entries; every ledger's entries and balances sum to zero (so the
settlement account mirrors the customers' money); every transaction has at least 2 entries
summing to zero and exactly its declared count; every entry sits in its account's and
transaction's ledger; every posted transaction has its succeeded audit event; and failed
events whose transaction committed after all (a lost acknowledgement) are reported, as are
audit events whose attempted transaction id is malformed (which would otherwise hide them).

## Posting

Deposits, withdrawals and transfers ([`services/posting.py`](src/ledger_api/services/posting.py))
each run in one database transaction ([ADR 0010](docs/adr/0010-posting-protocol.md)):

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
([ADR 0006](docs/adr/0006-idempotency-inside-the-posting-transaction.md)). PostgreSQL is the
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

User registration, account opening and every posting attempt are recorded in `audit_events`
([ADR 0011](docs/adr/0011-audit-trail.md)):

- **Succeeded** events are the last write of the business transaction: they commit with it or
  not at all, even when a deferred check rejects the transaction at `COMMIT`. If the audit
  write fails, the operation fails.
- **Rejected** (a business rule, with the error code as `reason`) and **failed** (contention
  or an unexpected error) events are written *after* the business transaction has rolled back
  and released its connection, in a new transaction on the same session. One operation never
  holds two connections.
- The audit writer never raises. If it cannot write, it logs an ERROR with the event (the log
  is the fallback sink), and the caller still gets its original error. Rejected and failed
  events are therefore best-effort.
- Details hold ids, amounts, currencies and error identifiers only: never an email address, an
  exception message or a request body. A claimed user that does not exist is stored as a NULL
  actor, with its id in `details.requested_user_id`.
- Reads, health checks and malformed requests are not audited.

Under connection-pool exhaustion the post-rollback audit write waits for a connection (up to
the pool timeout) before giving up, which delays the error response. This is accepted for now.

## Statements

`services.statements.get_statement` returns an owner's account history, newest first, with the
balance after each entry ([ADR 0012](docs/adr/0012-statement-ordering-and-reconciliation.md)),
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

## Testing

Tests run against real PostgreSQL, never SQLite, because the invariants above live in
triggers and constraints. The suite creates a disposable `<db>_test` database next to the one
in `DATABASE_URL`, builds it with the Alembic migrations, and wraps each test in a transaction
that is rolled back. As a safety guard, the suite refuses to drop any database whose name does
not end in `_test`.

```bash
make up      # PostgreSQL must be running
make test
```

Negative tests assert the exact constraint or trigger that rejected the data, not just that
some error occurred. API tests run each request in a fresh session on the test's rolled-back
connection, so services still open and commit their own transactions. An architecture test
fails if an account route is ever mounted outside `/users/{user_id}`.

Posting is tested at every level:

- **Failure injection** with real commits: a posting broken after its header, after its
  entries, midway through the balance updates, or at `COMMIT` leaves no trace.
- **Concurrency** (`-m concurrency`), on real connections with **retries disabled** so a
  deadlock cannot be retried away: racing withdrawals never overdraw, opposite transfers
  never deadlock, random concurrent transfers conserve money.
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

CI runs the same suite against a PostgreSQL service container and smoke-tests the Compose
stack.

## Architecture decisions

| ADR | Decision |
|---|---|
| [0001](docs/adr/0001-layered-modular-monolith.md) | Layered modular monolith on FastAPI and PostgreSQL |
| [0002](docs/adr/0002-money-as-integer-minor-units.md) | Money as integer minor units (`amount_minor`) |
| [0003](docs/adr/0003-per-currency-ledgers-and-signed-double-entry.md) | Per-currency ledgers and signed double-entry postings |
| [0004](docs/adr/0004-immutable-transactions-without-status.md) | Immutable transactions without a status column |
| [0005](docs/adr/0005-pessimistic-locking-under-read-committed.md) | Pessimistic row locking under READ COMMITTED |
| [0006](docs/adr/0006-idempotency-inside-the-posting-transaction.md) | Idempotency keys stored inside the posting transaction |
| [0007](docs/adr/0007-auditing-rejected-financial-attempts.md) | Auditing rejected attempts outside the financial transaction |
| [0008](docs/adr/0008-redis-only-for-rate-limiting.md) | Redis only for distributed rate limiting |
| [0009](docs/adr/0009-owner-scoped-access-and-error-responses.md) | Owner-scoped account access and `{code, detail}` error responses |
| [0010](docs/adr/0010-posting-protocol.md) | Posting protocol: sealed transactions, one sorted lock, whole-transaction retries |
| [0011](docs/adr/0011-audit-trail.md) | Audit trail: schema, write protocol, scope and the limits of trigger protection |
| [0012](docs/adr/0012-statement-ordering-and-reconciliation.md) | Commit-ordered entries, statements with computed running balances, independent reconciliation |

## Roadmap

1. ✅ Repository and development environment
2. ✅ Database and domain models
3. ✅ Users and accounts
4. ✅ Ledger and transaction posting
5. ✅ Audit trail, statements (service layer) and reconciliation
6. ✅ Idempotency and the HTTP posting API (deposits, withdrawals, transfers)
7. ✅ REST API: statements, single transactions, `Location` on postings
8. Authentication and authorization
9. Reliability
10. Security and observability (including least-privilege database roles)
