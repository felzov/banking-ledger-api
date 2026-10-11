# ADR 0013: Password login with opaque, database-backed bearer tokens

- Status: Accepted
- Date: 2026-10-11
- Amends: [ADR 0009](0009-owner-scoped-access-and-error-responses.md),
  [ADR 0011](0011-audit-trail.md)

## Context

Until Phase 8 the API trusted the `{user_id}` in the URL: anyone who knew a user id could act as
that user, including moving their money. ADR 0009 put every user-scoped route under one
`/users/{user_id}` router so that a single dependency could later enforce "the caller is
`{user_id}`". Phase 8 has to establish who the caller is without external infrastructure, and
without weakening the ledger, idempotency or audit guarantees or the service-owned transaction
boundaries (ADR 0001).

## Decision

### Credentials (passwords)

- Users register with an email and a password: `POST /users {email, password}`.
- **Policy** (NIST SP 800-63B): 12 to 128 characters **after NFKC normalization**, no
  composition rules. NFKC makes compatibility variants (full-width letters, ligatures, composed
  and decomposed accents) the same password. The raw input is bounded at 1,024 characters
  before normalization. A violation is `422 validation_error`, with the value never echoed.
- **Hashing:** Argon2id (argon2-cffi), RFC 9106's low-memory profile by default (3 passes,
  64 MiB, 4 lanes; `ARGON2_*` settings). `APP_ENV=production` refuses anything below OWASP's
  minimum (19 MiB, 2 passes). Verification accepts only the `$argon2id$` encoding and fails
  closed on anything else.
- **Never on the event loop, never holding a connection:** hashing and verification run in a
  worker thread (`asyncio.to_thread`), outside any database transaction.
- **Storage:** `user_credentials(user_id, password_hash, created_at, updated_at)` (migration
  0012), one row per user, the hash constrained to the Argon2id encoding. No credentials are
  backfilled: users created before Phase 8 cannot log in. A successful login re-hashes a
  password whose hash used older parameters.

### Sessions (opaque bearer tokens)

- `POST /sessions {email, password}` returns `201 {access_token, token_type: "bearer",
  expires_at}` with `Cache-Control: no-store`.
- **The token** is `lt_` followed by 32 bytes from the OS CSPRNG, base64url-encoded (256 bits).
  It is returned exactly once. **Only its SHA-256 digest is stored**, in
  `auth_sessions(id, user_id, token_hash, created_at, expires_at, revoked_at)`. A fast hash is
  enough because the token is uniformly random with 256 bits of entropy. A database dump yields
  no usable token, and **no signing key exists**.
- **Lifetime:** 30 minutes, absolute (`AUTH_SESSION_TTL_SECONDS`, default 1,800). `created_at`
  and `expires_at` are set from the database's `now()`, and every expiry check compares against
  `now()` in SQL, so application clocks never matter. A `CHECK` caps any session at 24 hours.
  There are no refresh tokens: a client logs in again.
- **Revocation:** `DELETE /sessions/current` revokes the session of the token it is called with
  (`204`). A trigger allows exactly one change to a session row, `revoked_at` from NULL to a
  time; every other update is refused. The logout statement matches only live sessions, so a
  repeated logout matches nothing and answers `401 invalid_token`.
- **Login failures are indistinguishable:** an unknown email, or a user without a password, is
  verified against a cached dummy Argon2id hash with the same parameters. Every failed login
  therefore costs exactly one verification and answers the identical `401 invalid_credentials`.

### Authentication on every protected request

- A dependency reads `Authorization: Bearer <token>`:
  - missing, empty, repeated or another scheme: `401 authentication_required`, with
    `WWW-Authenticate: Bearer`;
  - malformed, unknown, expired or revoked: `401 invalid_token`, with
    `WWW-Authenticate: Bearer error="invalid_token"` (RFC 6750). One answer for all four.
- The lookup (the token's digest among unrevoked, unexpired sessions) runs in **its own short
  transaction** on the request's session. It returns a plain `Principal(user_id, session_id)`,
  never an ORM object, after that transaction has ended. The route's services then open their
  own transactions as usual (ADR 0001); the request as a whole is never wrapped in a
  transaction.

### Authorization

- One dependency on the `/users/{user_id}` router requires `principal.user_id == {user_id}`,
  else **`403 forbidden`**. All nine user-scoped routes inherit it.
- It runs before each route's own dependencies, body validation and services. A cross-user
  request is therefore refused before any read, any idempotency-key claim and any posting, and
  nothing is written.
- 403 rather than 404: the check compares the path with the token and performs no lookup, so it
  reveals nothing about whether the other user exists. Inside the caller's own scope every 404
  of ADR 0009 is unchanged.
- Public, needing no token: `GET /health/live`, `GET /health/ready`, `POST /users`,
  `POST /sessions`. A test pins the full route inventory: a new route fails it until it is
  classified as public or protected.

### Audit (ADR 0011)

- `auth.login`: succeeded (actor: the user; details: the session id), or rejected with reason
  `invalid_credentials` (actor: the email's user when there is one, else none; the email itself
  is never recorded).
- `auth.logout`: succeeded (actor: the user; details: the session id).
- Not audited (logs only, like any unauthenticated request, ADR 0007): `401` and `403` on
  protected routes, failed logouts, and the authentication lookup itself.
- Never in an audit event, a log line or an error: a password, a token, or a token's digest.

## Alternatives considered

- **JWT access and refresh tokens:** stateless, but revocation needs server state anyway, plus
  key management, algorithm-confusion pitfalls and a new dependency.
- **An external identity provider (OAuth2/OIDC):** realistic for production, but external
  infrastructure this project avoids without need.
- **Long-lived API keys:** no login, long-lived secrets.
- **404 for cross-user requests:** the check needs no lookup, so 403 reveals nothing and is
  clearer for clients.
- **Paths without `{user_id}` (`/me/...`):** would break every client; the user-scoped router
  was designed for exactly this one dependency (ADR 0009).
- **Standard-library scrypt instead of Argon2id:** no dependency, but argon2-cffi is OWASP's
  first recommendation and its hashes carry their own parameters (re-hash on login).

## Consequences

- The caller's identity, not the URL, now decides whose money moves. Idempotency keys and
  audit actors on user-scoped routes are always those of an authenticated user.
- Each protected request costs one indexed lookup on `auth_sessions` (by token digest).
- **Not implemented yet** (documented gaps, not controls):
  - rate limiting and lockout against password guessing and credential stuffing (Phase 10,
    ADR 0008). Each guess costs one Argon2id verification; there is no lockout, by design;
  - cleanup of expired sessions, a per-user session limit, listing sessions, "revoke all";
  - a least-privilege database role (Phase 10). The application still connects as a superuser,
    which can disable the triggers (ADR 0004);
  - HTTPS/TLS termination. Bearer tokens must only travel over TLS; the local Compose stack is
    plain HTTP on 127.0.0.1;
  - password change and reset, email verification, MFA, roles.
- Users registered before Phase 8 cannot log in (no credentials were backfilled).
- Registration still answers `409` for a registered email, which allows enumerating addresses
  (ADR 0008); login does not add a second way to do so.
