# ADR 0016: Rate limiting: four limits in Redis, a login throttle, fail closed where it protects

- Status: Accepted
- Date: 2026-10-23
- Completes: [ADR 0008](0008-redis-only-for-rate-limiting.md)
- Amends: [ADR 0013](0013-authentication-and-authorization.md)

## Context

ADR 0008 chose Redis, used only for shared rate-limit counters, and named the abuse targets:
login (credential stuffing: each guess costs one Argon2id verification, and nothing limited
them, ADR 0013), registration (a `409` for a registered email allows enumerating addresses) and
postings (idempotency-key flooding). It left open how each endpoint behaves when Redis is
unavailable. An in-process counter was rejected there: several workers or replicas would each
count separately.

## Decision

### Four limits

| Limit | Counted per | Counts | Default | Redis unavailable |
|---|---|---|---|---|
| `login_address` | client address | every `POST /sessions` | 20 / minute | refused: `503` |
| `login_failures` | email address | logins since the email's last successful login | 10 / 15 minutes | refused: `503` |
| `registration_address` | client address | every `POST /users` | 10 / hour | refused: `503` |
| `postings_user` | authenticated user | every deposit, withdrawal and transfer, replays included | 60 / minute | allowed, WARNING |

All are settings (`LOGIN_ATTEMPTS_PER_MINUTE`, `LOGIN_FAILURES_PER_EMAIL`,
`LOGIN_FAILURE_WINDOW_SECONDS`, `REGISTRATIONS_PER_HOUR`, `POSTINGS_PER_MINUTE`).

- **Over a limit:** `429 rate_limited` in the usual `{code, detail}` body, with `Retry-After`
  in whole seconds until the window ends. The same answer whichever limit refused; which one,
  is in the log line (its name only: never the address, email or user it counted).
- **Not audited.** A refused request changed nothing and ran no service, like any request
  refused at the door (ADR 0007). Auditing refusals would turn a flood of them into a flood of
  database writes.

### Fail closed where the limit is the defence

Login and registration answer `503 temporarily_unavailable` while Redis cannot be reached: those
limits are what stops password guessing and email enumeration, and running without them
silently would remove the defence exactly when nobody is watching. Postings stay available:
that limit only bounds an authenticated user's load, and a Redis outage must not stop money
from moving. A posting that went unlimited logs a WARNING with the request's id.
`/health/ready` keeps checking PostgreSQL only, so a Redis outage never takes the API out of
rotation: logins are refused, everything else works.

### A login throttle per email, not a lockout

Once an email has had `LOGIN_FAILURES_PER_EMAIL` login attempts since its last successful login,
within the window, **every** further login for it is refused, correct password included, until
the window ends. A successful login clears the count, so in effect it counts failures.

- **Counted before the password is checked**, in the same atomic `INCR` that decides: an attempt
  over the limit is refused there, computing no hash and writing no audit event. The first
  version checked the count first and counted the failure only after Argon2: every attempt in
  flight passed the check before any was counted, and 40 concurrent guesses against a limit of 3
  were all checked (found in review; a test now sends exactly that and expects 3 checks).

- Unknown and registered emails are counted alike (the count is keyed by the email, not by a
  user), and the `429` is the same, so the throttle reveals nothing about which emails exist.
- A throttle ends by itself. A lockout that needs an administrator would let anyone lock any
  user out for good by failing on purpose; this limits that to one window. NIST SP 800-63B
  asks for a limit on failed attempts per account, which this is.

### Counters

- **Fixed window from the first hit:** `INCR`, then `EXPIRE … NX` so that only the first hit
  sets the window, in one `MULTI`/`EXEC`. Redis's clock ends the window; application clocks
  never matter. `INCR` is atomic, so concurrent requests are counted exactly.
- **Keys:** `<namespace>:rl:<limit>:<subject>`. An email is keyed by its SHA-256 (of the
  canonical form): Redis holds no email address in clear. IPv6 clients are counted per `/64`
  (a host usually holds a whole `/64`); an IPv4 address seen through a dual-stack socket counts
  as itself. `RATE_LIMIT_NAMESPACE` lets several deployments, or test runs, share one Redis.
- **The client address** is the peer uvicorn reports. Behind a reverse proxy that is the
  proxy's own address, unless uvicorn trusts it (`--forwarded-allow-ips`) and takes the client
  from `X-Forwarded-For`; anyone else's `X-Forwarded-For` is ignored.
- **Fail fast:** a 250 ms socket timeout and no retries (redis-py retries with backoff by
  default). An error Redis answers counts as unavailable too.
- **Compose:** `redis:8-alpine`, no persistence (a restart only resets counters), 64 MB with
  `volatile-ttl` eviction (every key has a TTL), no host port. The API reaches it on the
  Compose network; it holds no database credential.

### Off unless configured, required in production

Without `REDIS_URL` there are no limits (development, the test suite). `APP_ENV=production`
refuses to start without it, like it refuses weak Argon2 parameters. The suite enables limits
only where a test asks for them, against its own Redis (`TEST_REDIS_URL`, never `REDIS_URL`).

## Alternatives considered

- **In-process counters:** no dependency, but per process (ADR 0008).
- **Sliding window or token bucket in a Lua script:** smoother at window edges, harder to read
  and test. A fixed window lets up to about twice a limit through around a window boundary,
  acceptable at these limits.
- **Fail closed everywhere:** Redis would become a single point of failure for moving money.
  **Fail open everywhere:** a Redis outage would remove the login defence.
- **Hard lockout per account:** see above (denial of service against any user).
- **Limits per `(email, address)` pair:** less lockout of the real user, but a distributed
  attack against one account would not be limited at all.
- **Auditing refusals:** a security signal, but a write per refused request.
- **Valkey instead of Redis:** a BSD-licensed drop-in; either works, nothing here depends on
  the difference.

## Consequences

- Login and registration depend on Redis being reachable; postings, reads and health checks
  do not.
- A client behind a shared address (an office NAT) shares one budget per limit. Behind a reverse
  proxy that uvicorn does not trust, all clients share the proxy's.
- Anyone can throttle a known email for one window by failing on purpose; its owner gets `429`
  until the window ends.

## Known limitations

- Fixed windows: up to about twice a limit around a window boundary.
- A distributed attack with many addresses is limited per email (`login_failures`), not per
  address.
- No limit on authentication of protected routes: a guessed bearer token is hopeless (256 bits,
  ADR 0013), each attempt costs one indexed lookup.
- Redis has no password in the Compose stack (no host port, Compose network only). A deployment
  where other clients reach Redis needs `rediss://` with a password, given in `REDIS_URL`.
