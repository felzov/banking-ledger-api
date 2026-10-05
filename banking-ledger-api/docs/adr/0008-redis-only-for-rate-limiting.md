# ADR 0008: Redis only for distributed rate limiting (Phase 10)

- Status: Accepted
- Date: 2026-10-05

## Context

Several endpoints are abuse targets: login (credential stuffing), registration (email
enumeration, since a duplicate email returns 409), and `POST /transactions` (idempotency-key
flooding). An in-process counter is wrong once there are multiple uvicorn workers or
replicas, because each process counts separately.

## Decision

- Add Redis in Phase 10, used **only** for shared rate-limit counters.
- Never use Redis for idempotency or any financial state (see ADR 0006).
- `POST /users` with an existing email returns 409. Rate limiting is the mitigation for the
  enumeration risk this creates.

## Consequences

- Redis becomes a runtime dependency only once it solves a real problem.
- Behavior when Redis is unavailable (fail open or fail closed per endpoint) must be decided
  in Phase 10.
