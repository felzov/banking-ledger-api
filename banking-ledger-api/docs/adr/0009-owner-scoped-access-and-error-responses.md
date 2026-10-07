# ADR 0009: Owner-scoped account access and error responses

- Status: Accepted
- Date: 2026-10-07

## Context

Phase 3 exposes users and customer accounts over HTTP before authentication exists
(Phase 8). Broken object-level authorization (BOLA: user A reading user B's account by ID) is
the most common API vulnerability, and it is usually introduced by a handler that looks an
object up by ID alone. The design must make that mistake hard to write, both now and after
authentication is added. Phase 3 also needs a first, consistent error format.

## Decision

### Owner-scoped access

- Every route that acts on one user's data is mounted under `/users/{user_id}`, on a single
  router built in `create_app`. Phase 8 adds one router-level dependency there, requiring the
  caller to be `{user_id}`; every current and future route under it inherits the check.
- Customer accounts are looked up only by owner: `get_owned_account(session, *, owner_id,
  account_id)` filters on both columns. There is no unscoped "get account by id" for API use.
- Another user's account, a system account and a nonexistent account all produce the same
  `404 account_not_found`. A 403 would confirm that the ID exists.
- `owner_id` and `account_id` are keyword-only everywhere: both are UUIDs, and a positional
  swap would type-check and be a BOLA bug.
- Clients never choose the owner (it comes from the URL), the account kind (always
  `customer`) or the balance (always starts at 0). Request models forbid extra fields.
- An architecture test fails the build if an account route is mounted outside
  `/users/{user_id}`.

### Errors

- Business-rule failures are domain exceptions (`domain/errors.py`) in two categories,
  `NotFoundError` and `ConflictError`, each with a stable `code` and a safe message. They
  carry no HTTP knowledge; `api/errors.py` maps categories to 404 and 409.
- Every error body is `{"code": ..., "detail": ...}`. Validation errors are
  `422 {"code": "validation_error", "detail": [{type, loc, msg}]}`: the submitted `input` and
  the `ctx` are never returned, so a password will never be echoed back from Phase 8 on.
- Uniqueness and existence rules are enforced by named database constraints, with no
  SELECT beforehand. Services translate exact constraint names only
  (`uq_users_email`, `uq_accounts_user_id_ledger_id`, `fk_accounts_user_id_users`). Any other
  `IntegrityError` and any unexpected exception become a generic
  `500 {"code": "internal_error"}`; the traceback goes to the server log only.

### Email addresses

- One canonical form is stored: trimmed, ASCII-only, syntax-checked with `email-validator`
  (no DNS lookups), lowercased. No provider-specific rules (Gmail dots and `+tags`).
- ASCII-only is a deliberate MVP boundary: Python and PostgreSQL lowercase some Unicode
  characters differently (U+0130), and Unicode admits look-alike addresses.

## Alternatives considered

- **Flat paths (`/accounts/{id}`)**: natural once requests carry an identity, but in Phase 3
  such a lookup could not be scoped to an owner: an IDOR by design that Phase 8 would have to
  remember to fix in every handler.
- **A placeholder identity header (`X-User-Id`)**: looks like authentication without being
  any. Rejected.
- **403 for another user's account**: reveals which IDs exist.
- **SELECT-before-INSERT for duplicates and missing users**: racy, and duplicates the rules
  the database already enforces.
- **RFC 9457 problem details**: more than the project needs today; `{code, detail}` can grow
  into it.

## Consequences

- **The API is unauthenticated until Phase 8.** Anyone who knows a user ID can act as that
  user; UUIDv7 IDs are not secrets. Phase 3 guarantees that an account is only ever reached
  through its true owner (consistency of ownership), not who the caller is (identity). The
  service must not be exposed to untrusted clients before Phase 8.
- Account statements, balances and transfers (later phases) must reuse the owner-scoped
  lookup.
- Internationalized email addresses need a new decision (NFC normalization, collation)
  before they can be accepted.
- A duplicate email returns 409, which allows email enumeration; ADR 0008's rate limiting is
  the mitigation.
