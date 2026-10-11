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

## Amendment (Phase 7, 2026-10-11)

- The statement (`GET .../accounts/{account_id}/statement`) and the single-transaction read
  (`GET .../accounts/{account_id}/transactions/{transaction_id}`) live under `/users/{user_id}`
  and reuse the owner-scoped account lookup, **first**: someone else's, a system or a missing
  account is `account_not_found`, whatever the cursor or transaction id.
- A transaction is then looked up among that account's entries only. A transaction that does
  not exist, belongs to other accounts, or is the user's own posting on a different account is
  the same `404 transaction_not_found` (new `NotFoundError`), so the response never reveals
  whether a transaction id exists elsewhere.
- Every `201` posting carries `Location` to that transaction on the caller's account, built
  from the parsed path parameters and the service's result, never from raw client text.
- **Two 422 bodies.** `validation_error` (FastAPI) has `detail` as a list of
  `{type, loc, msg}`; business rules and header checks have `detail` as a string. Routes that
  can return both (the postings and the statement) document `422` as either schema; the others
  document only `validation_error`, which is all they can return. A test validates real 422
  bodies against the documented schema.
- Still unauthenticated until Phase 8; these reads are as exposed as every other route.

## Amendment (Phase 8, 2026-10-11): authentication enforced (ADR 0013)

- **The API is authenticated.** The router-level dependency this ADR planned now exists: every
  route under `/users/{user_id}` requires a valid bearer token, and the caller must be
  `{user_id}`. The consequence "unauthenticated until Phase 8" no longer applies.
- **Another user's id is `403 forbidden`, no longer a `404`.** `GET /users/{other}`,
  `/users/{other}/accounts`, and every route under another user's id, existing or not, answer
  403. The check compares the path with the token without a lookup, so it reveals nothing about
  whether that user exists. Without a valid token the answer is `401` (`authentication_required`
  or `invalid_token`).
- **Inside the caller's own scope, nothing changes:** someone else's account, a system account
  and a missing account are still the same `404 account_not_found`, through the owner-scoped
  lookup; `transaction_not_found` and `destination_account_not_found` are unchanged.
- `user_not_found` can no longer be reached through the API: the path's user is always the
  authenticated caller, who exists.
- The architecture tests are joined by an explicit route inventory: every route is classified
  as public or protected, and every protected route is tested for 401 and 403.
