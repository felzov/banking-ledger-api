# banking-ledger-api

A production-oriented banking backend built around a **double-entry ledger**: ACID money
movement, concurrency control, idempotent APIs, and an append-only audit trail.

> **Status: complete (all 10 phases).**
> Users register with a password and log in for a 30-minute bearer token. Every user-scoped
> route requires that token, and only its owner may use it on their own data. Deposits,
> withdrawals and transfers require an `Idempotency-Key`: a retried request never moves money
> twice, and an unknown COMMIT outcome is recoverable. An unreachable database answers `503`,
> not `500`, and cancelled requests are tested to leave the ledger consistent (see
> [Reliability](#reliability)). Statements and single transactions are read from the owner's
> side. Every attempt is recorded in an append-only audit trail. Login, registration and postings
> are rate limited in Redis ([Security](#security)); every response carries an `X-Request-ID`
> that its JSON log lines and audit events share ([Observability](#observability)). The full
> design is documented in the [ADRs](docs/adr/).

> [!WARNING]
> **Not production-ready.** Authentication, authorization and rate limits are implemented (see
> [Security](#security)), but several controls a public deployment needs are not:
> - **no HTTPS**: the local stack is plain HTTP on 127.0.0.1, and bearer tokens must only
>   travel over TLS;
> - **database roles are separated** (the API runs as an unprivileged `ledger_app`, see
>   [Database roles](#database-roles)), but credentials come from a local `.env` file, not a
>   secrets manager.
>
> Do not expose it to untrusted networks.

All data is synthetic. No real financial information, credentials or personal data are used.

## Technology stack

| Concern | Choice |
|---|---|
| Language | Python 3.14 |
| Web framework | FastAPI, Pydantic v2 |
| Database | PostgreSQL 18, SQLAlchemy 2.1 (async, asyncpg driver), Alembic migrations |
| Rate limits | Redis 8, for counters only (redis-py, asyncio) |
| Tooling | uv, ruff, mypy (strict), pytest, pre-commit (including gitleaks) |
| Runtime | Docker, Docker Compose |

## Local setup

Prerequisites: Docker with Compose v2+, [uv](https://docs.astral.sh/uv/), and GNU Make.

```bash
cd banking-ledger-api
cp .env.example .env        # synthetic local-only values
make install                # create .venv from uv.lock
make up                     # start PostgreSQL, create the roles, migrate, start the API
curl localhost:8000/health/ready
```

`make up` starts five services. The database ones start only once the previous one has
succeeded, each with its own database identity ([Database roles](#database-roles)):

1. `db`, PostgreSQL, until healthy;
2. `bootstrap`, one-shot, as the admin: creates or reconciles the roles
   (`python -m ledger_api.admin.bootstrap`);
3. `migrate`, one-shot, as `ledger_owner`: `alembic upgrade head`;
4. `redis`, the rate limits' counters, until healthy (no host port, no persistence);
5. `api`, as `ledger_app`, once the migrations completed and Redis is healthy.

**Existing volume** (created before Phase 10, where everything belongs to the admin): **back
it up first** ([below](#back-up-before-upgrading-an-existing-database)). Then add
`LEDGER_OWNER_PASSWORD` and `LEDGER_APP_PASSWORD` to `.env` (see `.env.example`), and run
`make up`. The bootstrap transfers every object in `public` to `ledger_owner`, then migration
0013 grants `ledger_app` its privileges; the data is untouched. Without those two variables
Compose refuses to start and names the missing one.

Interactive API docs: <http://localhost:8000/docs>

To enable the git hooks, run this once from the repository root:

```bash
banking-ledger-api/.venv/bin/pre-commit install
```

### Back up before upgrading an existing database

Before `make up`, `make migrate` or any other upgrade of a database that already holds data,
take a backup and rehearse restoring it with `scripts/rehearse-backup-restore.sh`:

```bash
mkdir -m 700 ~/ledger-backups   # outside the repository, owned by you, mode 700
scripts/rehearse-backup-restore.sh --backup-dir ~/ledger-backups             # plan only
scripts/rehearse-backup-restore.sh --backup-dir ~/ledger-backups --execute   # back up, rehearse
```

- **Default (no `--execute`):** checks the arguments and the backup directory and prints the
  plan. It runs no Docker or database command.
- **`--execute`:** dumps the development database `ledger` read-only (`pg_dump`, plus the roles
  without passwords), restores the dump into a new, uniquely named database on the disposable
  test cluster (`test-db`, which must be running and marked: run `make test` once), compares the
  copy with the source (schema, ownership, privileges, sequences, data) and checks the ledger's
  integrity. It then drops that copy, only after re-checking the containers, the cluster and the
  database's OID. Nothing is written to the development database.
- **The backup files are kept** in the backup directory (mode 600): they are the backup.
  Protect them like the database itself.
- **A logical dump of one database, not a physical backup of the cluster.** Database-level
  permissions and settings (e.g. who may `CONNECT`) are not in it; after a restore, the role
  bootstrap re-applies the database-level revokes. The script rehearses a restore into a
  separate database; it never restores over an existing one.

## Environment variables

| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `APP_ENV` | api | `development` | One of `development`, `test`, `production`. Validated at startup |
| `DATABASE_URL` | api, alembic, tests | (required) | `postgresql+asyncpg://…`. Validated at startup. Not set in `.env`: Compose and the Makefile build it per process, with `ledger_app`'s credentials for the API and `ledger_owner`'s for the migrations |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | db, bootstrap | (required) | The database admin (the PostgreSQL bootstrap superuser). Given only to the `db` service and the role bootstrap |
| `LEDGER_OWNER_PASSWORD` / `LEDGER_APP_PASSWORD` | bootstrap, migrate / api | (required) | Passwords of `ledger_owner` and `ledger_app`. 16+ characters; written into URLs unescaped, so letters, digits and `. _ ~ -` only |
| `ADMIN_DATABASE_URL` | bootstrap | (required) | The admin's URL, for the role bootstrap only. Not set in `.env`: Compose and `make bootstrap` build it |
| `POSTGRES_PORT` | compose | `5432` | Host port, bound to 127.0.0.1 |
| `API_PORT` | compose | `8000` | Host port, bound to 127.0.0.1 |
| `AUTH_SESSION_TTL_SECONDS` | api | `1800` | Absolute lifetime of a login session (30 minutes; 60–86,400) |
| `ARGON2_TIME_COST` / `ARGON2_MEMORY_COST_KIB` / `ARGON2_PARALLELISM` | api | `3` / `65536` / `4` | Argon2id password-hashing cost (RFC 9106 low-memory profile). With `APP_ENV=production`, anything below 19,456 KiB or 2 passes is refused at startup |
| `REDIS_URL` | api | unset | `redis://` or `rediss://`: the rate limits' counters. Unset: no rate limits; `APP_ENV=production` refuses to start without it. Compose sets it for the API |
| `RATE_LIMIT_NAMESPACE` | api | `ledger` | Prefix of every counter key, so deployments can share a Redis |
| `LOGIN_ATTEMPTS_PER_MINUTE` | api | `20` | `POST /sessions` per client address |
| `LOGIN_FAILURES_PER_EMAIL` / `LOGIN_FAILURE_WINDOW_SECONDS` | api | `10` / `900` | Logins per email since its last successful login (in effect, failures) before it is throttled until the window ends |
| `REGISTRATIONS_PER_HOUR` | api | `10` | `POST /users` per client address |
| `POSTINGS_PER_MINUTE` | api | `60` | Deposits, withdrawals and transfers per user, together |
| `LOG_LEVEL` | api | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`, for the application and its libraries |
| `TEST_REDIS_URL` | tests | (set by `make test`) | The rate-limit tests' Redis (`test-redis`). Never `REDIS_URL`: the rest of the suite runs without limits |

## Development commands

| Command | Action |
|---|---|
| `make up` / `make down` | Start or stop the stack. Data is kept in the `pgdata` volume |
| `make logs` | Follow container logs |
| `make bootstrap` | Create or reconcile the database roles, as the admin (the `db` service must be up) |
| `make migrate` | Apply migrations from the host, as `ledger_owner` (after `make bootstrap`) |
| `make revision m="..."` | Create a new, empty migration |
| `make db-shell` | Open `psql` in the database container |
| `make test` | Run the test suite on its own disposable PostgreSQL and Redis (`test-db`, `test-redis`, started if needed) |
| `make smoke` | Start-up smoke test in an isolated Compose project (own volume, ports and image), removed afterwards |
| `make lint` / `make fmt` | Lint and format check, or auto-fix |
| `make typecheck` | `mypy --strict` |
| `make check` | All quality gates |

## API

| Method | Path | Description |
|---|---|---|
| GET | `/health/live` | Liveness: the process is serving requests. It checks no dependencies |
| GET | `/health/ready` | Readiness: PostgreSQL answers `SELECT 1` within 2 s. Otherwise `503 {"status": "unavailable"}` |
| POST | `/users` | **Public.** Register: `{"email", "password"}` (12–128 characters). `201` + `Location`; the password is never returned; `409` if the email exists |
| POST | `/sessions` | **Public.** Log in: `{"email", "password"}`. `201 {"access_token", "token_type": "bearer", "expires_at"}`; `401 invalid_credentials` |
| DELETE | `/sessions/current` | Log out: revokes the bearer token of the request. `204` |
| GET | `/users/{user_id}` | Get a user |
| POST | `/users/{user_id}/accounts` | Open a customer account: `{"currency": "GBP" \| "EUR"}`. Balance starts at 0. `409` if the user already has one in that currency |
| GET | `/users/{user_id}/accounts` | List the user's accounts: `{"items": [...]}` |
| GET | `/users/{user_id}/accounts/{account_id}` | Get one of the user's accounts |
| GET | `/users/{user_id}/accounts/{account_id}/statement?limit=&cursor=` | The account's history, newest first, with the balance after each entry. Keyset pagination: `limit` 1–100 (default 50), `cursor` = the previous page's `next_cursor` |
| GET | `/users/{user_id}/accounts/{account_id}/transactions/{transaction_id}` | One transaction as it appears on this account (the same shape as a statement entry) |
| POST | `/users/{user_id}/accounts/{account_id}/deposits` | Money in from outside: `{"amount_minor"}`. Requires `Idempotency-Key`. `201` + `Location` |
| POST | `/users/{user_id}/accounts/{account_id}/withdrawals` | Money out to outside: `{"amount_minor"}`. Requires `Idempotency-Key`. `201` + `Location` |
| POST | `/users/{user_id}/accounts/{account_id}/transfers` | To any customer account in the same currency: `{"destination_account_id", "amount_minor"}`. Requires `Idempotency-Key`. `201` + `Location` |

Every route under `/users/{user_id}` requires `Authorization: Bearer <access_token>`, and
`{user_id}` must be the token's user (see [Authentication](#authentication)). The health
endpoints, registration and login are public.

There is deliberately no separate `/balance` endpoint (`GET .../accounts/{account_id}` returns
`balance_minor`) and no separate transaction list (the statement is the account's history).

Interactive documentation, including every request and response schema:
<http://localhost:8000/docs>.

### Example

```bash
curl -s -X POST localhost:8000/users -H 'content-type: application/json' \
  -d '{"email": "Alice@Example.com", "password": "correct horse battery staple"}'
# 201 {"id": "<uuid>", "email": "alice@example.com", "created_at": "..."}

curl -s -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"email": "alice@example.com", "password": "correct horse battery staple"}'
# 201 {"access_token": "lt_...", "token_type": "bearer", "expires_at": "..."}
TOKEN='lt_...'   # valid for 30 minutes

curl -s -X POST localhost:8000/users/<user_id>/accounts -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOKEN" -d '{"currency": "GBP"}'
# 201 {"id": "<uuid>", "currency": "GBP", "balance_minor": 0, "created_at": "..."}

curl -si -X POST localhost:8000/users/<user_id>/accounts/<account_id>/deposits \
  -H 'content-type: application/json' -H "Authorization: Bearer $TOKEN" \
  -H 'Idempotency-Key: deposit-2026-10-10-001' -d '{"amount_minor": 2500}'
# 201 {"transaction_id": "<uuid>", "kind": "deposit", "account_id": "<uuid>",
#      "amount_minor": 2500, "balance_after_minor": 2500, "created_at": "..."}

# Location: /users/<user_id>/accounts/<account_id>/transactions/<transaction_id>

# The same request again (e.g. after a timeout): nothing is posted twice.
# 201, same body, same Location, plus the header  Idempotent-Replayed: true

curl -s -H "Authorization: Bearer $TOKEN" \
  'localhost:8000/users/<user_id>/accounts/<account_id>/statement?limit=2'
# 200 {"account_id": "<uuid>", "balance_minor": 2500,
#      "entries": [{"entry_id": "<uuid>", "transaction_id": "<uuid>", "kind": "deposit",
#                   "amount_minor": 2500, "balance_after_minor": 2500, "created_at": "..."}],
#      "next_cursor": null}

curl -s -H "Authorization: Bearer $TOKEN" \
  localhost:8000/users/<user_id>/accounts/<account_id>/transactions/<transaction_id>
# 200 the same entry as on the statement

curl -s -o /dev/null -w '%{http_code}\n' -X DELETE localhost:8000/sessions/current \
  -H "Authorization: Bearer $TOKEN"
# 204: the token is revoked; using it again answers 401 invalid_token
```

### Authentication

- **Register, then log in.** `POST /users` takes an email and a password. The password must be
  12–128 characters after Unicode NFKC normalization, with no composition rules. `POST /sessions`
  exchanges the email and password for an opaque bearer token (`lt_...`, 256 random bits),
  shown exactly once, with `Cache-Control: no-store`.
- **Sessions last 30 minutes,** absolutely (`expires_at`, set by the database's clock). There is
  no refresh token: log in again. `DELETE /sessions/current` revokes the token immediately.
- **Every route under `/users/{user_id}` needs `Authorization: Bearer <token>`**, and
  `{user_id}` must be the token's user.
  - No usable header (missing, empty, repeated, another scheme): `401 authentication_required`.
  - A malformed, unknown, expired or revoked token: `401 invalid_token`. One answer for all.
  - Every `401` carries `WWW-Authenticate: Bearer` (`invalid_token` adds `error="invalid_token"`).
  - A valid token on another user's id, existing or not: `403 forbidden`, before anything is
    read or written. It never claims an idempotency key or moves money.
- **Login failures are indistinguishable:** an unknown email and a wrong password both answer
  `401 invalid_credentials`, and cost the same work.
- **Never stored or logged:** the password (only its Argon2id hash is stored) and the token
  (only its SHA-256 digest is stored). Neither ever appears in a response other than the login,
  in a log line, or in the audit trail.

### Rules

- **Accounts are only reachable through their owner.** Every user-scoped route lives under
  `/users/{user_id}`, which must be the caller. Within the caller's own scope, another user's
  account, a system (settlement) account and a missing account all return the same `404`, so
  responses never reveal which IDs exist.
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
| 401 | `authentication_required` | No usable `Authorization: Bearer` header on a protected route or logout |
| 401 | `invalid_token` | The bearer token is malformed, unknown, expired or revoked |
| 401 | `invalid_credentials` | Login with an unknown email or a wrong password (indistinguishable) |
| 403 | `forbidden` | A valid token on another user's `/users/{user_id}/...` (whether or not that user exists) |
| 404 | `account_not_found` | Within the caller's own scope: the account is missing, someone else's, or a system account |
| 404 | `destination_account_not_found` | A transfer's destination is missing or not a customer account |
| 404 | `transaction_not_found` | The transaction has no entry on this account (it does not exist, or belongs to other accounts) |
| 409 | `email_already_registered`, `account_already_exists` | Uniqueness rules (enforced by the database) |
| 422 | `validation_error` | Invalid request, including a password outside the policy. `detail` lists `{type, loc, msg}`; submitted values (passwords included) are never echoed |
| 422 | `invalid_amount`, `same_account_transfer`, `currency_mismatch`, `insufficient_funds` | Posting rules |
| 422 | `idempotency_key_mismatch` | The `Idempotency-Key` was already used for a different request |
| 422 | `invalid_statement_limit`, `invalid_statement_cursor` | A `limit` outside 1–100; a cursor that is not an entry of this account. (A non-integer `limit` or a malformed cursor is `validation_error`) |
| 422 | `invalid_idempotency_key` | The header is not one value of 1–255 visible ASCII characters (never echoed) |
| 428 | `idempotency_key_required` | A posting without an `Idempotency-Key` header |
| 429 | `rate_limited` | A rate limit was exceeded (login, registration, postings; [Security](#security)). `Retry-After` gives the seconds until its window ends |
| 503 | `temporarily_unavailable` | Lock or statement timeout, or contention retries exhausted; the database could not be reached, or no pooled connection was free in time (any route); a posting's connection was lost before its `COMMIT` was sent; Redis could not be reached for a login or registration (their rate limits fail closed). The request changed nothing (a failed audit event may still be recorded). Retry later (postings: with the same `Idempotency-Key`) |
| 503 | `outcome_unknown` | The connection was lost during COMMIT: the posting may or may not have been applied. **Retry with the same `Idempotency-Key`** |
| 500 | `internal_error` | Anything unexpected, including a statement whose balance does not match its entries (refused rather than returned). No details are returned; the traceback goes to the server log |

Every response, errors included, carries an `X-Request-ID` header ([Observability](#observability)).

Two body shapes share `422`, and the OpenAPI schema documents both where both can occur:
`validation_error` carries a list of `{type, loc, msg}`; every other code carries a string.

**`503` versus `500` for database failures.** `503 temporarily_unavailable` is reserved for
failures where the request's own changes cannot have been applied: no connection was ever
obtained, or (for postings) the connection was lost before `COMMIT` was sent. Everything PostgreSQL itself
reports (a constraint, a misconfigured database name or password, any other server error)
keeps answering `500`, as do unrelated errors. A connection lost in the middle of a
non-posting request (login, registration, account opening, reads) also stays `500`: only the
posting service tracks whether `COMMIT` was already sent. See [Reliability](#reliability) and
[ADR 0014](docs/adr/0014-failure-handling-unavailability-cancellation-and-shutdown.md).

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
transaction and its key reference each other, and
[`0012`](migrations/versions/0012_add_authentication.py) adds password credentials, bearer-token
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
([ADR 0004](docs/adr/0004-immutable-transactions-without-status.md)). Since Phase 10 the API is
neither: it connects as `ledger_app`, which owns nothing and holds only migration 0013's
privileges, so these rules hold against the application itself, not only against its bugs
([Database roles](#database-roles)).

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

User registration, account opening, every posting attempt, logins (successful and rejected)
and logouts are recorded in `audit_events`
([ADR 0011](docs/adr/0011-audit-trail.md), [ADR 0013](docs/adr/0013-authentication-and-authorization.md)).
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
  an audit record; see [Audit events and fallback log lines](#audit-events-and-fallback-log-lines)
  for what each line does and does not prove.
- A failure to obtain a database connection is recorded with reason `temporarily_unavailable`,
  the code the caller receives, when the event can still be written.
- Details hold ids, amounts, currencies and error identifiers only: never an email address, an
  exception message or a request body. A claimed user that does not exist is stored as a NULL
  actor, with its id in `details.requested_user_id`.
- Reads, health checks and malformed requests are not audited.

Under connection-pool exhaustion the post-rollback audit write waits for a connection (up to
the pool timeout) before giving up, which delays the error response. This is accepted for now.

## Reliability

How the service behaves when something other than a business rule fails
([ADR 0014](docs/adr/0014-failure-handling-unavailability-cancellation-and-shutdown.md); unknown
`COMMIT` outcomes: [ADR 0006](docs/adr/0006-idempotency-inside-the-posting-transaction.md)).

### Retry with the same `Idempotency-Key`

A posting's outcome lives in the database, not in the HTTP response. Whenever the response is
missing or inconclusive (a client timeout, `503 outcome_unknown`, `503
temporarily_unavailable`, a dropped connection), retry the **same request with the same key**.
If the original committed, the retry replays it (`201`, `Idempotent-Replayed: true`, the
original transaction). If nothing was committed, the retry is a fresh attempt: it may succeed,
be rejected or fail like any request, and if the original is still finishing its `COMMIT` it may
wait and then answer `503 temporarily_unavailable` (retry again, still with the same key). In
every case the posting is applied at most once per key. A **new** key is a new request:
retrying with a different key after a lost response can move the money twice.

The service retries only deadlocks and serialization failures, inside one request
([ADR 0010](docs/adr/0010-posting-protocol.md)). There is no HTTP-layer retry: retrying is the
client's decision, with its key.

### A posting can commit even when the client never receives the response

A posting is committed the moment PostgreSQL applies its `COMMIT`, together with its succeeded
audit event. Nothing that happens afterwards undoes it: a lost acknowledgement, a client that
disconnects, a cancelled request, a killed process. The client then sees `503
outcome_unknown`, a dropped connection, or nothing at all, while the money has moved. The
converse also holds: a posting that does not reach a successful `COMMIT` changes no ledger
data (no transaction, entries, balance change or idempotency key); at most its rejected or
failed audit event is recorded. A statement shows the account's postings but cannot tie one to
a request whose response was lost; a retry with the same key can, which is why the rule above
is the way to learn the outcome.

### Client disconnects and cancelled requests

- **A client disconnect does not cancel the request** with the current server stack (uvicorn,
  and no middleware that cancels on disconnect). This is a property of that stack, not something
  the application enforces, and it was verified by a probe against uvicorn 0.54, not by the test
  suite. The posting runs to completion, commits or is rejected as usual, and is audited as
  usual. The client learns the result by retrying with the same key.
- **A cancelled request** (`asyncio.CancelledError`, e.g. a server that cancels in-flight
  requests while shutting down) always propagates, and no audit event is written for the
  cancellation itself:
  - **before `COMMIT`**: nothing is committed. SQLAlchemy rolls the transaction back, or ends
    the backend so that PostgreSQL does; the connection is returned to the pool or closed. A
    posting waiting for a row lock stops waiting; it cannot acquire the lock later and carry on.
  - **during `COMMIT`**: the outcome is unknown. If PostgreSQL had applied the `COMMIT`, the
    posting and its succeeded event stand (tested); if it had not, nothing is committed. Either
    way a WARNING `posting cancelled during COMMIT: its outcome is unknown to the caller
    (attempted transaction <uuid>)` is logged (never the key), and a same-key retry tells which.
    A replay logs nothing: it wrote nothing.
  - **while a rejected or failed event is being written**: the event's fate is logged; see
    [Audit events and fallback log lines](#audit-events-and-fallback-log-lines).

### When the network stalls during `COMMIT`

A known limitation. If a reply from PostgreSQL never arrives (packets lost without a reset),
the request waits, holding its pooled connection, until the connection fails: how long that
takes depends on the operating system's TCP settings, not on this application.
PostgreSQL's `lock_timeout` and `statement_timeout` do not help once the server is idle, there
is no client-side timeout yet, and cancelling the request does not free it (observed with a
proxy that withholds `COMMIT`'s reply). When the connection does fail, a `COMMIT` in that state
is reported as `outcome_unknown` (a posting's earlier statement as `temporarily_unavailable`,
other requests' as `internal_error`), and the client retries with the same key. A client-side command timeout and TCP keepalives are deferred
([ADR 0014](docs/adr/0014-failure-handling-unavailability-cancellation-and-shutdown.md)).

### Shutdown

As configured today (nothing here goes beyond `compose.yaml`, the `Dockerfile` and uvicorn's
defaults):

- `make down` or `docker compose stop` sends **SIGTERM** to the `api` container and, if it is
  still running after Docker's default **10 seconds**, **SIGKILL**: `compose.yaml` sets no
  `stop_grace_period` and no `stop_signal`. (In Compose the API runs with `--reload`; uvicorn's
  reloader passes the SIGTERM on to its worker process.)
- uvicorn runs without `--timeout-graceful-shutdown` (neither in `compose.yaml` nor in the
  image's `CMD`). On a SIGTERM it stops accepting connections and waits for in-flight requests
  with no time limit of its own, then the application's lifespan disposes the database engine;
  it does not cancel requests. (A second Ctrl+C in a terminal forces an immediate exit instead.)
- So a request that finishes within the grace period completes normally. One that does not is
  killed with the process: its connection drops; once PostgreSQL notices, it rolls back anything
  uncommitted; and a posting caught inside its `COMMIT` has an unknown outcome, which a
  same-key retry resolves. **Graceful shutdown is not guaranteed beyond the grace period.**
- These points come from the configuration and from uvicorn's source; the uvicorn behaviour was
  also checked with an in-process server. The full `docker compose stop` sequence is not
  exercised by the test suite.

### Audit events and fallback log lines

A rejected or failed event is written after its business transaction
([Audit trail](#audit-trail)). When that write cannot complete, the writer logs an ERROR line
carrying the event's fields instead:

| What you find | What it means |
|---|---|
| A row in `audit_events` | The event is persisted. Succeeded events only ever exist this way: they commit with the operation or not at all |
| `audit event not written (cancelled): ...` | The request was cancelled before the event's insert completed: the event is **not** in `audit_events` |
| `audit event not written (the session is still in a transaction): ...` | The writer refused to write (a caller bug): the event is **not** in `audit_events` |
| `audit event not written (<error class>): ...` | The write raised that error. Usually the insert failed or no connection was obtained (e.g. `TimeoutError`, `DatabaseUnavailableError`), and the event is not there. **Known gap:** if the error came from the event's own `COMMIT` (e.g. a lost connection), the row may exist despite the wording; check `audit_events` |
| `audit event possibly not written (cancelled during COMMIT): ...` | The insert completed but its `COMMIT` was interrupted by a cancellation. The row **may exist**: check `audit_events` before treating the event as missing |

Each line is a JSON log line ([Observability](#observability)) and names the event's
`request_id`. A log line is a fallback, not an audit record: it is not append-only, cannot be
queried with the events, and is only as durable as the process's stderr (`make logs` includes
the `api` container's output).

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
- **Cost (known limitation):** running balances are recomputed on every read. Each statement
  page scans the account's whole history, and a single-transaction read sums the account's
  entries up to that one, so both grow linearly with the account's history. Acceptable for the
  MVP; the documented upgrade path is persisted running balances with a gapless per-account
  sequence ([ADR 0012](docs/adr/0012-statement-ordering-and-reconciliation.md), "Alternatives
  considered").

## Security

**Implemented** ([ADR 0013](docs/adr/0013-authentication-and-authorization.md),
[ADR 0016](docs/adr/0016-rate-limiting.md)):

- Passwords: the 12–128-character policy after NFKC; Argon2id hashing, with production refusing
  parameters below OWASP's minimum; hashing and verification run in a worker thread, never
  while a database connection is held.
- Opaque bearer tokens with 256 random bits. Only their SHA-256 digest is stored, so there is no
  signing key to leak. A 30-minute absolute lifetime in database time, revocation on logout,
  and no refresh tokens.
- Authentication on every user-scoped route, and ownership (`{user_id}` must be the caller)
  enforced by one router-level dependency before any service runs. Cross-user requests are
  `403` and change nothing.
- Login failures that don't reveal whether an email is registered (same answer, same cost).
- No password, token or token digest in responses (apart from the login's token), logs, errors
  or audit details. Validation errors never echo submitted values.
- Owner-scoped lookups and identical 404s inside the caller's scope (ADR 0009); idempotency
  keys scoped to the authenticated user; database-enforced ledger, idempotency and audit
  invariants.
- **Rate limits**, counted in Redis so every worker and replica shares them (`429
  rate_limited` with `Retry-After`, not audited):

  | Limit | Per | Default | If Redis is unreachable |
  |---|---|---|---|
  | Login attempts | client address | 20 / minute | `503` (fail closed) |
  | Failed logins | email address | 10 attempts since the last successful login / 15 minutes, then the email is throttled until the window ends, correct password included; a success clears the count | `503` (fail closed) |
  | Registrations | client address | 10 / hour | `503` (fail closed) |
  | Postings | user | 60 / minute, replays included | not enforced, WARNING (fail open) |

  Each login is counted before its password is checked, so concurrent attempts cannot outrun
  the limit, and a throttled login computes no hash. Unknown and registered emails are counted
  alike, so the throttle reveals nothing about which emails exist; the price is that anyone can
  throttle a known email for one window. IPv6 clients count per `/64`. Redis holds counters only, with
  emails as SHA-256 digests. The client address is the peer uvicorn sees: behind a reverse
  proxy, uvicorn must trust it (`--forwarded-allow-ips`), or every client shares the proxy's
  budget.

**Not implemented** (deferred, so not controls this project provides today):

- **Session cleanup** (expired rows accumulate), a **per-user session limit**, listing sessions,
  and "revoke all sessions".
- **Production HTTPS.** The local stack serves plain HTTP on 127.0.0.1; a deployment needs TLS
  termination, since bearer tokens must never travel unencrypted.
- Password change and reset, email verification, MFA, roles and administrators.
- Registration still answers `409` for a registered email; the per-address limit bounds how
  fast addresses can be enumerated, it does not remove the signal.
- Redis has no password in the Compose stack (no host port; Compose network only). A deployment
  where anything else reaches Redis needs `rediss://` with credentials in `REDIS_URL`.

### Database roles

Three identities, each holding only what its job needs
([ADR 0015](docs/adr/0015-database-roles-and-least-privilege.md)):

| Role | Used by | Can | Cannot |
|---|---|---|---|
| admin (`POSTGRES_USER`) | `db`, the one-shot role bootstrap | everything (the PostgreSQL bootstrap superuser) | — used by nothing else |
| `ledger_owner` | migrations (`migrate`, `make migrate`) | own and change the schema | act as a superuser, create roles or databases, or serve the API (the startup guard refuses an owner) |
| `ledger_app` (via the group `ledger_runtime`) | the API | exactly migration 0013's grants: column-level `INSERT`/`UPDATE`, `SELECT` where read, `INSERT` only on `audit_events` | read audit events, change history (`UPDATE`/`DELETE`/`TRUNCATE`), choose sequence numbers, own or create anything, disable triggers |

- **Why migrations run as `ledger_owner`, not as a superuser:** the owner can create objects in
  `public`. A superuser session resolving names through `public` would run anything placed
  there (an operator, a function) with superuser privileges. `migrations/env.py` refuses to run
  as a superuser; only Python code (the test suite) can exempt itself.
- **Why the API uses `ledger_app`:** the append-only, double-entry and audit triggers bind every
  role except a superuser or an owner. The API is neither, so a bug or an attacker in the API
  cannot switch them off or read the audit trail.
- **Startup privilege guard:** every new database connection the API opens is checked on the
  server: superuser, `BYPASSRLS`, `CREATEROLE`, `CREATEDB`, `REPLICATION`, owning the database
  or anything in `public`, or membership (inherited or by `SET ROLE`) in a role that does,
  refuses it. Requests then answer `503 temporarily_unavailable`, `/health/ready` answers `503`,
  and a CRITICAL log line names the role and the reason (never a URL or password).
  `/health/live` keeps answering. A check that cannot complete refuses too.
- **Credentials per service:** only the bootstrap gets the admin's URL, only `migrate` the
  owner's, only `api` the app's (a test reads `compose.yaml` to keep it so; the smoke test
  inspects the running containers).

## Observability

([ADR 0017](docs/adr/0017-structured-logs-and-request-correlation.md))

- **JSON log lines** on stderr, one object per line, from the application, uvicorn and the
  libraries alike: `time` (UTC), `level`, `logger`, `message`, `request_id`, and for an
  exception `exc_type` and `traceback`. `LOG_LEVEL` sets the level. Only a fixed list of extra
  fields is ever written, and never a password, token, token digest or email address.
- **A request id per request**, generated by the server (a UUIDv7; a client's own
  `X-Request-ID` is ignored) and returned in the `X-Request-ID` header of every response,
  `500` included. Every log line written while serving the request carries it, uvicorn's
  traceback of an unhandled error included, and every audit event records it in `request_id`.
- **One access line per request** (`ledger_api.access`): method, path without the query
  string, status and `duration_ms`. uvicorn's own access log is silenced.

```json
{"time": "2026-10-23T11:03:01.777+00:00", "level": "INFO", "logger": "ledger_api.access", "message": "GET /health/live 200", "request_id": "01a1686d-b190-71e8-bc7d-93173bad0894", "method": "GET", "path": "/health/live", "status": 200, "duration_ms": 1.7}
```

To follow one request: take the `X-Request-ID` a client reports, find its lines in the logs,
and its events with `SELECT * FROM audit_events WHERE request_id = '…'` (as the admin: the API
cannot read the trail). uvicorn's first two startup lines are plain text: they are written
before the application configures logging.

## Testing

Tests run against real PostgreSQL, never SQLite, because the invariants above live in
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
  behind; after PostgreSQL has committed (a TCP proxy, [`tests/tcp_proxy.py`](tests/tcp_proxy.py),
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
| [0013](docs/adr/0013-authentication-and-authorization.md) | Password login (Argon2id) with opaque, database-backed bearer tokens; one router-level ownership check |
| [0014](docs/adr/0014-failure-handling-unavailability-cancellation-and-shutdown.md) | Failure handling: `503` for unreachable databases and pre-`COMMIT` connection loss, cancellation, audit fallback logs, shutdown as configured |
| [0015](docs/adr/0015-database-roles-and-least-privilege.md) | Database roles: admin bootstrap, `ledger_owner` migrations, `ledger_app` with column-level grants; migration and startup privilege guards |
| [0016](docs/adr/0016-rate-limiting.md) | Rate limiting: four limits in Redis, a per-email login throttle, fail closed for login and registration |
| [0017](docs/adr/0017-structured-logs-and-request-correlation.md) | JSON log lines and a server-generated request id shared by the response, the logs and the audit trail |

## Roadmap

1. ✅ Repository and development environment
2. ✅ Database and domain models
3. ✅ Users and accounts
4. ✅ Ledger and transaction posting
5. ✅ Audit trail, statements (service layer) and reconciliation
6. ✅ Idempotency and the HTTP posting API (deposits, withdrawals, transfers)
7. ✅ REST API: statements, single transactions, `Location` on postings
8. ✅ Authentication and authorization (password login, bearer tokens, ownership on every user-scoped route)
9. ✅ Reliability: database outages answer `503`, pre-`COMMIT` and cancellation behaviour tested, failure handling documented
10. ✅ Security and observability: least-privilege database roles and their guards, rate limits in Redis, JSON logs and request correlation
