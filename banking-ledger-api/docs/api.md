# API reference

Authentication, every route, the rules responses follow, and every error code. Back to the [README](../../README.md).

## Authentication

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
| 429 | `rate_limited` | A rate limit was exceeded (login, registration, postings; [Security](security.md)). `Retry-After` gives the seconds until its window ends |
| 503 | `temporarily_unavailable` | Lock or statement timeout, or contention retries exhausted; the database could not be reached, or no pooled connection was free in time (any route); a posting's connection was lost before its `COMMIT` was sent; Redis could not be reached for a login or registration (their rate limits fail closed). The request changed nothing (a failed audit event may still be recorded). Retry later (postings: with the same `Idempotency-Key`) |
| 503 | `outcome_unknown` | The connection was lost during COMMIT: the posting may or may not have been applied. **Retry with the same `Idempotency-Key`** |
| 500 | `internal_error` | Anything unexpected, including a statement whose balance does not match its entries (refused rather than returned). No details are returned; the traceback goes to the server log |

Every response, errors included, carries an `X-Request-ID` header ([Observability](operations.md#observability)).

Two body shapes share `422`, and the OpenAPI schema documents both where both can occur:
`validation_error` carries a list of `{type, loc, msg}`; every other code carries a string.

**`503` versus `500` for database failures.** `503 temporarily_unavailable` is reserved for
failures where the request's own changes cannot have been applied: no connection was ever
obtained, or (for postings) the connection was lost before `COMMIT` was sent. Everything PostgreSQL itself
reports (a constraint, a misconfigured database name or password, any other server error)
keeps answering `500`, as do unrelated errors. A connection lost in the middle of a
non-posting request (login, registration, account opening, reads) also stays `500`: only the
posting service tracks whether `COMMIT` was already sent. See [Reliability](failure-modes.md) and
[ADR 0014](adr/0014-failure-handling-unavailability-cancellation-and-shutdown.md).
