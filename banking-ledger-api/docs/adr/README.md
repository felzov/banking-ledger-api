# Architecture decisions

Back to the [README](../../../README.md).

- [ADR 0001 — Layered modular monolith on FastAPI and PostgreSQL](0001-layered-modular-monolith.md)
- [ADR 0002 — Money as integer minor units (`amount_minor`)](0002-money-as-integer-minor-units.md)
- [ADR 0003 — Per-currency ledgers and signed double-entry postings](0003-per-currency-ledgers-and-signed-double-entry.md)
- [ADR 0004 — Immutable transactions without a status column](0004-immutable-transactions-without-status.md)
- [ADR 0005 — Pessimistic row locking under READ COMMITTED](0005-pessimistic-locking-under-read-committed.md)
- [ADR 0006 — Idempotency keys stored inside the posting transaction](0006-idempotency-inside-the-posting-transaction.md)
- [ADR 0007 — Auditing rejected attempts outside the financial transaction](0007-auditing-rejected-financial-attempts.md)
- [ADR 0008 — Redis only for distributed rate limiting](0008-redis-only-for-rate-limiting.md)
- [ADR 0009 — Owner-scoped account access and `{code, detail}` error responses](0009-owner-scoped-access-and-error-responses.md)
- [ADR 0010 — Posting protocol: sealed transactions, one sorted lock, whole-transaction retries](0010-posting-protocol.md)
- [ADR 0011 — Audit trail: schema, write protocol, scope and the limits of trigger protection](0011-audit-trail.md)
- [ADR 0012 — Commit-ordered entries, statements with computed running balances, independent reconciliation](0012-statement-ordering-and-reconciliation.md)
- [ADR 0013 — Password login (Argon2id) with opaque, database-backed bearer tokens; one router-level ownership check](0013-authentication-and-authorization.md)
- [ADR 0014 — Failure handling: `503` for unreachable databases and pre-`COMMIT` connection loss, cancellation, audit fallback logs, shutdown as configured](0014-failure-handling-unavailability-cancellation-and-shutdown.md)
- [ADR 0015 — Database roles: admin bootstrap, `ledger_owner` migrations, `ledger_app` with column-level grants; migration and startup privilege guards](0015-database-roles-and-least-privilege.md)
- [ADR 0016 — Rate limiting: four limits in Redis, a per-email login throttle, fail closed for login and registration](0016-rate-limiting.md)
- [ADR 0017 — JSON log lines and a server-generated request id shared by the response, the logs and the audit trail](0017-structured-logs-and-request-correlation.md)
