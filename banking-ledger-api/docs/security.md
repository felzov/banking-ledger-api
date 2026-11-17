# Security

What is protected, how, and the three database roles. Back to the [README](../../README.md).

**Implemented** ([ADR 0013](adr/0013-authentication-and-authorization.md),
[ADR 0016](adr/0016-rate-limiting.md)):

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

**Not implemented:** see [Known limitations](limitations.md).

### Database roles

Three identities, each holding only what its job needs
([ADR 0015](adr/0015-database-roles-and-least-privilege.md)):

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
