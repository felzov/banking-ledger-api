# Failure modes

What happens when something other than a business rule fails. Back to the [README](../../README.md).

## Reliability

How the service behaves when something other than a business rule fails
([ADR 0014](adr/0014-failure-handling-unavailability-cancellation-and-shutdown.md); unknown
`COMMIT` outcomes: [ADR 0006](adr/0006-idempotency-inside-the-posting-transaction.md)).

### Retry with the same `Idempotency-Key`

A posting's outcome lives in the database, not in the HTTP response. Whenever the response is
missing or inconclusive (a client timeout, `503 outcome_unknown`, `503
temporarily_unavailable`, a dropped connection), retry the **same request with the same key**.
If the original committed, the retry replays it (`201`, `Idempotent-Replayed: true`, the
original transaction). If nothing was committed, the retry is a fresh attempt: it may succeed,
be rejected or fail like any request, and if the original is still finishing its `COMMIT` it may
wait and then answer `503 temporarily_unavailable` (retry again, still with the same key). In
every case the posting is applied at most once per key. A **new** key is a new request:
retrying with a different key after a lost response can move the money twice.

The service retries only deadlocks and serialization failures, inside one request
([ADR 0010](adr/0010-posting-protocol.md)). There is no HTTP-layer retry: retrying is the
client's decision, with its key.

### A posting can commit even when the client never receives the response

A posting is committed the moment PostgreSQL applies its `COMMIT`, together with its succeeded
audit event. Nothing that happens afterwards undoes it: a lost acknowledgement, a client that
disconnects, a cancelled request, a killed process. The client then sees `503
outcome_unknown`, a dropped connection, or nothing at all, while the money has moved. The
converse also holds: a posting that does not reach a successful `COMMIT` changes no ledger
data (no transaction, entries, balance change or idempotency key); at most its rejected or
failed audit event is recorded. A statement shows the account's postings but cannot tie one to
a request whose response was lost; a retry with the same key can, which is why the rule above
is the way to learn the outcome.

### Client disconnects and cancelled requests

- **A client disconnect does not cancel the request** with the current server stack (uvicorn,
  and no middleware that cancels on disconnect). This is a property of that stack, not something
  the application enforces, and it was verified by a probe against uvicorn 0.54, not by the test
  suite. The posting runs to completion, commits or is rejected as usual, and is audited as
  usual. The client learns the result by retrying with the same key.
- **A cancelled request** (`asyncio.CancelledError`, e.g. a server that cancels in-flight
  requests while shutting down) always propagates, and no audit event is written for the
  cancellation itself:
  - **before `COMMIT`**: nothing is committed. SQLAlchemy rolls the transaction back, or ends
    the backend so that PostgreSQL does; the connection is returned to the pool or closed. A
    posting waiting for a row lock stops waiting; it cannot acquire the lock later and carry on.
  - **during `COMMIT`**: the outcome is unknown. If PostgreSQL had applied the `COMMIT`, the
    posting and its succeeded event stand (tested); if it had not, nothing is committed. Either
    way a WARNING `posting cancelled during COMMIT: its outcome is unknown to the caller
    (attempted transaction <uuid>)` is logged (never the key), and a same-key retry tells which.
    A replay logs nothing: it wrote nothing.
  - **while a rejected or failed event is being written**: the event's fate is logged; see
    [Audit events and fallback log lines](#audit-events-and-fallback-log-lines).

### When the network stalls during `COMMIT`

A known limitation. If a reply from PostgreSQL never arrives (packets lost without a reset),
the request waits, holding its pooled connection, until the connection fails: how long that
takes depends on the operating system's TCP settings, not on this application.
PostgreSQL's `lock_timeout` and `statement_timeout` do not help once the server is idle, there
is no client-side timeout yet, and cancelling the request does not free it (observed with a
proxy that withholds `COMMIT`'s reply). When the connection does fail, a `COMMIT` in that state
is reported as `outcome_unknown` (a posting's earlier statement as `temporarily_unavailable`,
other requests' as `internal_error`), and the client retries with the same key. A client-side command timeout and TCP keepalives are deferred
([ADR 0014](adr/0014-failure-handling-unavailability-cancellation-and-shutdown.md)).

### Shutdown

As configured today (nothing here goes beyond `compose.yaml`, the `Dockerfile` and uvicorn's
defaults):

- `make down` or `docker compose stop` sends **SIGTERM** to the `api` container and, if it is
  still running after Docker's default **10 seconds**, **SIGKILL**: `compose.yaml` sets no
  `stop_grace_period` and no `stop_signal`. (In Compose the API runs with `--reload`; uvicorn's
  reloader passes the SIGTERM on to its worker process.)
- uvicorn runs without `--timeout-graceful-shutdown` (neither in `compose.yaml` nor in the
  image's `CMD`). On a SIGTERM it stops accepting connections and waits for in-flight requests
  with no time limit of its own, then the application's lifespan disposes the database engine;
  it does not cancel requests. (A second Ctrl+C in a terminal forces an immediate exit instead.)
- So a request that finishes within the grace period completes normally. One that does not is
  killed with the process: its connection drops; once PostgreSQL notices, it rolls back anything
  uncommitted; and a posting caught inside its `COMMIT` has an unknown outcome, which a
  same-key retry resolves. **Graceful shutdown is not guaranteed beyond the grace period.**
- These points come from the configuration and from uvicorn's source; the uvicorn behaviour was
  also checked with an in-process server. The full `docker compose stop` sequence is not
  exercised by the test suite.

### Audit events and fallback log lines

A rejected or failed event is written after its business transaction
([Audit trail](ledger-guarantees.md#audit-trail)). When that write cannot complete, the writer logs an ERROR line
carrying the event's fields instead:

| What you find | What it means |
|---|---|
| A row in `audit_events` | The event is persisted. Succeeded events only ever exist this way: they commit with the operation or not at all |
| `audit event not written (cancelled): ...` | The request was cancelled before the event's insert completed: the event is **not** in `audit_events` |
| `audit event not written (the session is still in a transaction): ...` | The writer refused to write (a caller bug): the event is **not** in `audit_events` |
| `audit event not written (<error class>): ...` | The write raised that error. Usually the insert failed or no connection was obtained (e.g. `TimeoutError`, `DatabaseUnavailableError`), and the event is not there. **Known gap:** if the error came from the event's own `COMMIT` (e.g. a lost connection), the row may exist despite the wording; check `audit_events` |
| `audit event possibly not written (cancelled during COMMIT): ...` | The insert completed but its `COMMIT` was interrupted by a cancellation. The row **may exist**: check `audit_events` before treating the event as missing |

Each line is a JSON log line ([Observability](operations.md#observability)) and names the event's
`request_id`. A log line is a fallback, not an audit record: it is not append-only, cannot be
queried with the events, and is only as durable as the process's stderr (`make logs` includes
the `api` container's output).
