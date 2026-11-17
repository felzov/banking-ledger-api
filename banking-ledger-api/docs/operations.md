# Operations

Running the stack, configuring it, backing it up and following a request through the logs. Back to the [README](../../README.md).

## Quick start

Requires Docker with Compose v2+, [uv](https://docs.astral.sh/uv/) and GNU Make.

```bash
cd banking-ledger-api
cp .env.example .env        # synthetic local-only values
make install                # create .venv from uv.lock
make up                     # PostgreSQL, the roles, the migrations and Redis, then the API
make check                  # ruff, mypy --strict and the tests, on their own PostgreSQL and Redis
curl localhost:8000/health/ready
```

Interactive API docs: <http://localhost:8000/docs>. To enable the git hooks, run this once from
the repository root:

```bash
banking-ledger-api/.venv/bin/pre-commit install
```

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

## Database

`make up` starts five services. The database ones start only once the previous one has
succeeded, each with its own database identity ([Database roles](security.md#database-roles)):

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

## Observability

([ADR 0017](adr/0017-structured-logs-and-request-correlation.md))

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

## Project layout

```text
banking-ledger-api/
  src/ledger_api/
    domain/        pure rules and types (no I/O): money, postings, idempotency, audit records,
                   passwords, tokens, rate-limit keys
    data/          SQLAlchemy models, the engine (timeouts, privilege guard), owner-scoped
                   queries, reconciliation, Redis counters
    services/      use cases: the only layer that opens and commits transactions (ADR 0001)
    api/           FastAPI routes (thin), dependencies, error mapping, request middleware
    observability/ JSON log lines and the request id
    admin/         the role bootstrap, run by an operator, never imported by the application
    config.py      settings from the environment
    main.py        app factory
  migrations/      Alembic environment and versions 0001-0014
  scripts/         start-up smoke test, backup and restore rehearsal
  tests/
    unit/          no database or Redis
    api/           HTTP, through the application
    integration/   against a real PostgreSQL and Redis
  docs/adr/        architecture decision records
```
