# ADR 0015: Database roles and least privilege

- Status: Accepted
- Date: 2026-10-14
- Amends: [ADR 0004](0004-immutable-transactions-without-status.md),
  [ADR 0011](0011-audit-trail.md), [ADR 0012](0012-statement-ordering-and-reconciliation.md),
  [ADR 0013](0013-authentication-and-authorization.md)

## Context

Until Phase 10 every process (the API, the migrations, the test suite) connected as one role:
the superuser that the PostgreSQL image creates from `POSTGRES_USER`. The ledger's guarantees
live in triggers (append-only rows, the double-entry check at `COMMIT`, ordered entries, the
idempotency link, revoke-only sessions), and triggers bind ordinary sessions only: a superuser
or a table's owner can disable or drop them, or skip them with
`session_replication_role = replica`. The earlier ADRs recorded this honestly: the rules held
against application bugs, **not against the role the application ran as**. A bug, or an
attacker able to run SQL through the API, could rewrite history and read every audit event.

## Decision

### Three identities

| Role | Kind | Used by | Holds |
|---|---|---|---|
| admin (`POSTGRES_USER`) | the bootstrap superuser | the `db` service and the role bootstrap only | everything |
| `ledger_owner` | login role | migrations | owns every object in `public`; `CREATE` on `public`; `CONNECT` |
| `ledger_runtime` | `NOLOGIN` group | grants target | migration 0013's privileges; `USAGE` on `public` |
| `ledger_app` | login role, member of `ledger_runtime` (`INHERIT TRUE, SET FALSE`) | the API | `CONNECT`; what it inherits; nothing it owns |

No role but the admin is a superuser, has `CREATEDB`, `CREATEROLE`, `REPLICATION` or
`BYPASSRLS`, or any membership other than `ledger_app` in `ledger_runtime`.

### The role bootstrap (`python -m ledger_api.admin.bootstrap`)

Run as the admin, before migrations, in one transaction (role DDL is transactional): creates or
reconciles the three roles (passwords as client-computed SCRAM verifiers, never plaintext),
revokes `CONNECT`/`TEMPORARY` on the database and every privilege on `public` from `PUBLIC`,
and transfers every object in `public` from the admin to `ledger_owner` (an existing volume;
`REASSIGN OWNED` refuses the bootstrap superuser, so object by object). It refuses, and changes
nothing, on anything it cannot explain: a target role that is a superuser or has another
membership or owns what it must not, default privileges that would grant to the runtime roles or
`PUBLIC`, objects owned by another role, privileges from another grantor or with grant option.
Its own session pins `search_path` to `pg_catalog, pg_temp`. Idempotent.

### Migration 0013: the application's privileges

`ledger_runtime` gets, table by table and column by column, exactly what the application's
queries use: `SELECT` where it reads (its triggers read as the invoker too), `INSERT` on the
columns it writes (never `created_at`, `occurred_at`, `balance_minor`, `revoked_at` or
`sequence_number`), `UPDATE` on `accounts.balance_minor`, `user_credentials.password_hash` and
`updated_at`, `auth_sessions.revoked_at`; on `audit_events` `INSERT` only (the insert carries no
`RETURNING`). `EXECUTE` on the sequence helper of migration 0007 only; `PUBLIC` loses `EXECUTE`
on the trigger functions (a trigger fires without it). No `DELETE`, `TRUNCATE`, `REFERENCES`,
`TRIGGER`, sequence privilege or grant option. The migration refuses unless `ledger_runtime`
exists as a `NOLOGIN`, non-superuser role with no membership. Its downgrade fails closed.

### Start order and credentials per service

`db` (healthy) → `bootstrap` (admin) → `migrate` (`ledger_owner`) → `api` (`ledger_app`), each
waiting for the previous one to succeed. Each service receives only its own credentials: the
admin's URL only reaches the bootstrap, the owner's only the migrations, the API holds only
`ledger_app`'s URL. `make bootstrap` and `make migrate` do the same from the host.

### Guards

- **Migrations refuse a superuser** (`migrations/env.py`). A superuser session would resolve the
  names in its statements through `public`, where `ledger_owner` may create objects: an
  operator such as `public.=(oid, regnamespace)` is an exact match where `pg_catalog`'s is not,
  and would run with superuser privileges. The only exemption is
  `config.attributes["allow_superuser_migrations"]`, settable from Python only (the test suite
  migrating its own disposable databases): no command-line option, `-x` argument or
  environment variable reaches it.
- **The API refuses a privileged connection** (`data/engine.py`, a pool `connect` event): on
  every new physical connection, the server reports for `session_user` and `current_user`
  whether the role is a superuser, has `BYPASSRLS`, `CREATEROLE`, `CREATEDB` or `REPLICATION`,
  owns the database or anything in `public`, or is a member (inheriting or only by `SET ROLE`)
  of a role that is or does, or of a predefined role such as `pg_read_all_data`. Any of these,
  or a check that cannot complete, discards the connection with `PrivilegedDatabaseRoleError`:
  requests answer `503 temporarily_unavailable` with the usual generic body, `/health/ready`
  answers `503`, a CRITICAL log line names the role and the reasons (never a URL or password),
  and `/health/live` still answers (ADR 0014). The check runs with `search_path` pinned.
  `create_app(..., privilege_guard=False)` is a Python-only hook for tests that connect as a
  superuser on purpose; the served application (`uvicorn --factory`) always has the guard on.

### Tests on a disposable cluster only

Roles are cluster-wide, so the test suite runs on its own PostgreSQL (`test-db`, tmpfs) and
refuses to create a database or role unless the environment declares a disposable cluster AND
the server itself carries the marker `ledger.test_cluster = 'disposable'` (set out of band on the
throwaway server, never through `DATABASE_URL`) AND the database is not migrated.

## Alternatives considered

- **Keep one superuser, rely on the triggers.** Rejected: the guarantees would hold only against
  bugs, not against the identity the application runs as.
- **The owner role also serves the API.** Rejected: an owner can disable its tables' triggers.
- **Table-level grants, or `ALTER DEFAULT PRIVILEGES`.** Rejected: column-level grants stop
  backdating and chosen sequence numbers by privilege; explicit grants per migration keep each
  privilege reviewable, and a catalog test fails on any table without a decision.
- **A configurable runtime group name in the migration.** Rejected: a migration is a frozen
  snapshot; the test suite gets the fixed name on its disposable cluster instead.
- **Check privileges once at startup.** Rejected: the application deliberately starts while the
  database is down (ADR 0014); checking every new connection also catches a role changed later.
- **An environment variable to skip the guards.** Rejected: any production setting could flip
  it. The exemptions are Python-only.

## Consequences

- A bug or an attacker in the API can no longer change history, disable a trigger, read the
  audit trail, choose an entry's position or backdate a record: the database refuses with
  `42501` before any trigger is reached.
- **What it does not stop:** the API's role may still insert new, valid postings (balanced,
  sealed, ordered, linked): the database does not know who is authenticated. Audit and
  reconciliation remain the controls there. A `SECURITY DEFINER` posting function that checks
  the caller would be the next step; deferred.
- `ledger_owner` can still disable triggers on objects it owns; its credentials are therefore
  given only to the migrations.
- `REVOKE` removes only its own grantor's grants: the bootstrap refuses database and schema
  privileges from other grantors. For tables, the catalog tests check the schema the migrations produce, on disposable databases; a live database is not checked automatically (deferred).
- Other databases on the same server keep PostgreSQL's default `PUBLIC CONNECT`. Role-level
  settings (`ALTER ROLE ... SET`) of the three roles are not checked. The bootstrap needs a
  superuser admin, which managed services may not offer.
- Credentials still come from a local `.env`, not a secrets manager.
- `audit_events.request_id` is not writable by the API until request correlation adds it.
- Each new physical connection costs one catalog query; pooled connections are reused unchecked.
