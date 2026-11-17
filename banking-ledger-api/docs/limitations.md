# Known limitations

What the project does not do. Back to the [README](../../README.md).

- **No HTTPS.** The local stack serves plain HTTP on 127.0.0.1; a deployment needs TLS
  termination, since bearer tokens must never travel unencrypted.
- **No session cleanup** (expired rows accumulate), no per-user session limit, no listing of
  sessions and no "revoke all sessions".
- **No** password change or reset, email verification, MFA, roles or administrators.
- Registration still answers `409` for a registered email; the per-address limit bounds how
  fast addresses can be enumerated, it does not remove the signal.
- Redis has no password in the Compose stack (no host port; Compose network only). A deployment
  where anything else reaches Redis needs `rediss://` with credentials in `REDIS_URL`.
- **Credentials** come from a local `.env` file, not a secrets manager.
- **Rate limits** use fixed windows: up to about twice a limit can pass around a window
  boundary. Anyone can throttle a known email for one window by failing on purpose. Behind a
  reverse proxy, uvicorn must trust it (`--forwarded-allow-ips`), or every client shares the
  proxy's budget ([ADR 0016](adr/0016-rate-limiting.md)).
- **Logs** are as durable as the process's stderr; shipping them somewhere queryable is the
  deployment's job. uvicorn's first two startup lines are plain text
  ([ADR 0017](adr/0017-structured-logs-and-request-correlation.md)).
- **The audit trail** can still be rewritten by the admin superuser and `ledger_owner`, neither of
  which the API holds; rejected and failed events are best-effort
  ([Audit trail](ledger-guarantees.md#audit-trail), [ADR 0011](adr/0011-audit-trail.md)).
- **Two currencies**, GBP and EUR, each with its own ledger; accounts in different currencies
  cannot transact with each other.
- **Users registered before Phase 8** cannot log in: no credentials were backfilled
  ([ADR 0013](adr/0013-authentication-and-authorization.md)).
