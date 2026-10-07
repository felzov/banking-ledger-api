# banking-ledger-api

A production-oriented banking backend built around a **double-entry ledger**: ACID money
movement, concurrency control, idempotent APIs, and an append-only audit trail.

> **Status: Phase 2 of 10. Database and domain models.**
> The PostgreSQL schema, its financial invariants and the async persistence layer exist.
> Money-moving logic (postings, transfers) and business endpoints do not exist yet. The full
> design is documented in the [ADRs](docs/adr/).

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

## Database

One migration ([`0001_initial_schema`](migrations/versions/0001_initial_schema.py)) creates the
schema and seeds one ledger per supported currency (GBP, EUR), each with its external
settlement account.

```
users 1 ── 0..* accounts *── 1 ledgers 1 ── * transactions
                  │                              │
                  └──── * ledger_entries * ──────┘
```

| Table | Purpose |
|---|---|
| `users` | Identity only (lowercase, unique email) |
| `ledgers` | One per currency: the boundary money cannot cross |
| `accounts` | `customer` (owned by a user, one per currency) or `system` (settlement, one per ledger). `balance_minor` is a cached projection of the entries |
| `transactions` | A posted transaction: `deposit`, `withdrawal` or `transfer`. Append-only |
| `ledger_entries` | One signed `amount_minor` on one account. Append-only |

Conventions: UUIDv7 primary keys; money is `BIGINT` minor units named `*_minor`; timestamps
are `timestamptz`; every foreign key is `ON DELETE RESTRICT`; constraint names follow a fixed
convention (`ck_`, `uq_`, `fk_`, `ix_`, `pk_`).

Invariants enforced by PostgreSQL itself, so every code path is bound by them:

| Invariant | Mechanism |
|---|---|
| Every transaction has at least 2 entries summing to 0 | Deferred constraint triggers, checked at `COMMIT` |
| Posted transactions and entries are never updated or deleted | `BEFORE UPDATE OR DELETE` triggers |
| An entry, its transaction and its account share one ledger (no cross-currency postings) | Composite foreign keys on `(…, ledger_id)` |
| Customer balances never go negative; settlement balances may | `CHECK (kind = 'system' OR balance_minor >= 0)` |
| One account per user per currency, one settlement account per ledger | Unique constraint, partial unique index |
| An account appears at most once per transaction | Unique `(transaction_id, account_id)` |

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
some error occurred. CI runs the same suite against a PostgreSQL service container and
smoke-tests the Compose stack.

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

## Roadmap

1. ✅ Repository and development environment
2. ✅ Database and domain models
3. Users and accounts
4. Ledger and transaction model
5. Money transfers and database transaction boundaries
6. Idempotency and concurrency
7. REST API
8. Authentication and authorization
9. Testing and reliability
10. Security, observability and documentation
