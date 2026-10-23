# ADR 0014: Failure handling: unreachable databases, cancelled requests and shutdown

- Status: Accepted
- Date: 2026-10-13
- Amends: [ADR 0010](0010-posting-protocol.md), [ADR 0011](0011-audit-trail.md)

## Context

Phase 9 audited how the service behaves when something other than a business rule fails.
Two gaps stood out:

- **A database outage was reported as a bug.** With PostgreSQL unreachable, every
  database-backed route answered `500 internal_error`, while `/health/ready` answered `503`.
  A client could not tell "retry later" from "this is a defect".
- **Cancellation was untested.** `asyncio.CancelledError` is a `BaseException`, so it bypasses
  every `except Exception` in the posting and audit code. Nothing showed what a cancelled
  request leaves behind.

These facts were established experimentally (SQLAlchemy 2.1.3, asyncpg 0.31.0, uvicorn 0.54)
and are pinned by tests:

- A failure to **establish** a connection (refused, unreachable, timed out) is raised by the
  driver as a **raw `OSError`**, not wrapped by SQLAlchemy. SQLAlchemy's `handle_error` event
  never sees it (at connect time it is invoked for DBAPI errors only).
- A **pool acquisition timeout** is `sqlalchemy.exc.TimeoutError`, not the builtin
  `TimeoutError`.
- A refusal the **server itself** reports at connect time (unknown database, bad credentials)
  is a `DBAPIError` with severity `FATAL`: a misconfiguration, not an outage.
- A connection lost **before** COMMIT and one lost **during** COMMIT raise the **same**
  client-side error: invalidated, with no server severity (e.g. `08003`). The error alone
  cannot say whether COMMIT was sent.
- In every cancellation case tested (after the key claim, in a lock wait, during an audit
  write), SQLAlchemy rolled back, or ended the backend so that PostgreSQL rolled back, and left
  no connection checked out. If PostgreSQL had already committed, the posting and its succeeded
  event stood together.
- With uvicorn 0.54 and no middleware that cancels on disconnect, a client disconnect does not
  cancel the request handler: the posting runs to completion. This is a property of the server
  stack, verified by a probe, not by the test suite.

## Decision

### 1. Connection failures that are provably "nothing happened" answer 503

| Failure | How it is recognised | Where | Answer |
|---|---|---|---|
| No connection could be established | `data.engine` registers a `do_connect` listener that opens the connection exactly as SQLAlchemy would and re-raises a connect-time `OSError` as `DatabaseUnavailableError` (a `ConnectionError`) | every route | `503 temporarily_unavailable` |
| No pooled connection within the pool timeout | `sqlalchemy.exc.TimeoutError` | every route | `503 temporarily_unavailable` |
| A connection lost before COMMIT was sent | client-side loss (`is_connection_lost`) while the posting's `committing` flag is false | postings only | `503 temporarily_unavailable` |
| A connection lost during COMMIT | client-side loss while `committing` is true | postings | `503 outcome_unknown` (unchanged, ADR 0006) |
| A connection lost mid-transaction, outside postings | | other services | `500 internal_error` (unchanged) |
| A server-reported error, at connect or later | `DBAPIError` with a server severity | every route | unchanged (`500`, or its existing translation) |

`do_connect` is the one place that knows an `OSError` came from opening a connection, so no
handler ever has to guess about a bare `OSError`: an unrelated one (a file, a socket the
application opens) still answers `500`. An acquisition failure means the request's transaction
never sent a statement, so it is safe to report as "nothing happened" on every route. A lost
connection is safe to report that way only before COMMIT, and only the posting service knows
when that is: it already tracks `committing` for the COMMIT outcome classification (ADR 0006).
Other services do not track it, so a mid-transaction loss there stays `500`.

No HTTP-layer retry is added. A posting is not retried in-process for these failures either:
the client decides, with its `Idempotency-Key`.

### 2. Before COMMIT and during COMMIT stay distinct

The posting service checks, in this order:

1. `committing` and not a definite rollback: `outcome_unknown` (the existing rule, unchanged).
2. Not `committing`, and an acquisition failure or a lost connection:
   `temporarily_unavailable`, not retried.
3. Everything else as before (contention, deadlock and serialization retries, raw re-raise).

### 3. Cancellation propagates, is never audited in the database, and is logged

`CancelledError` always reaches the caller. Nothing is written to the database while a request
is being cancelled: the write would be cancelled too, and the request is gone. Two log lines
record what would otherwise be invisible:

- `posting cancelled during COMMIT: its outcome is unknown to the caller (attempted transaction
  <uuid>)`, a WARNING, when a posting is cancelled while `committing` is true. Not for a replay:
  a replay's transaction wrote nothing (its claim found the key taken), so its COMMIT decides
  nothing. The idempotency key is never logged here.
- The audit writer's fallback line (next section) when a rejected or failed event's write is
  cancelled.

A posting cancelled before its COMMIT logs nothing: nothing of it was committed. (Its audit
write, if one was in progress, is the second case above.)

### 4. A persisted audit event, a confirmed loss, and an uncertain loss are different things

A rejected or failed event is written after the business transaction (ADR 0011). When that
write cannot complete, the writer logs an ERROR carrying the event's fields:

| Log line | Meaning |
|---|---|
| (none; the row exists) | The event is persisted in `audit_events` |
| `audit event not written (cancelled): ...` | Cancelled before the event's insert completed: it is not in `audit_events` |
| `audit event not written (<error class>): ...` | The write raised that error; usually the insert failed or no connection was obtained, and the event is not there. If the error came from the event's own COMMIT (e.g. a lost connection), the row may exist despite the wording (see Consequences) |
| `audit event possibly not written (cancelled during COMMIT): ...` | The insert completed and its COMMIT was interrupted by a cancellation: the row **may** exist. Check `audit_events` before treating it as missing |

A log line is a fallback, not an audit record: it is not append-only, not queryable with the
events, and only as durable as the process's stderr (no logging handler is configured, so
Python's last-resort handler prints WARNING and above; structured logging is Phase 10).
Succeeded events never depend on it: they commit with the operation or not at all.

### 5. Shutdown is whatever the process manager and uvicorn do; it is not configured here

As configured today:

- **Compose (`api` service):** `uvicorn ... --reload`. The service sets no `stop_grace_period`
  and no `stop_signal`, so `docker compose stop`/`down` sends **SIGTERM** and, after Docker's
  default 10 seconds, **SIGKILL**. With `--reload`, uvicorn's reloader receives the SIGTERM and
  passes it to the worker process.
- **Image `CMD`:** `uvicorn ... --factory`, no `--reload`, no `--timeout-graceful-shutdown`.
- **uvicorn** (`timeout_graceful_shutdown` unset): on a SIGTERM it stops accepting connections
  and waits for in-flight requests **without a time limit**, then runs the application's
  lifespan shutdown (`engine.dispose()`); it does not cancel requests. A second SIGINT (Ctrl+C)
  forces an immediate exit.

So a request that finishes within the stop grace period completes normally. One that does not
is killed with the process: its connection drops; once PostgreSQL notices, it rolls back
anything not yet committed; and a posting caught inside its COMMIT has an unknown outcome,
which a same-key retry resolves. No graceful-shutdown guarantee exists beyond that. (If a
uvicorn graceful timeout were set, uvicorn would cancel the remaining requests and run the
lifespan shutdown without waiting for them: section 3 then applies.) These statements rest on
the configuration and on uvicorn's source, with the uvicorn part also checked on an in-process
server; the `docker compose stop` sequence itself is not exercised by the test suite.

## Alternatives considered

- **A global `OSError` → 503 handler:** would turn unrelated bugs into "retry later".
- **SQLAlchemy's `handle_error` event:** never sees connect-time `OSError`s.
- **Classifying mid-transaction losses in every service:** needs per-service COMMIT tracking;
  without it, a COMMIT-time loss would be misreported as "nothing happened".
- **Treating server-reported transient refusals as 503** (`57P03` starting up, `53300` too many
  connections): kept on the existing path, so the rule needs no SQLSTATE list. Can be added as
  a curated list.
- **Auditing cancellations in the database** (a shielded write): a database write during
  cancellation can itself hang on the connection being torn down; a log line cannot.
- **A client-side statement timeout (asyncpg `command_timeout`) and TCP keepalives now:** they
  would bound a stalled network (below), but their errors need the same probe-and-classify work
  as this ADR before COMMIT. Deferred.
- **HTTP-layer retries:** a retry belongs to the client, which holds the `Idempotency-Key`.

## Consequences

- An outage, an exhausted pool, or a connection lost before a posting's COMMIT answer
  `503 temporarily_unavailable`, the same body as contention. Server-reported errors, bugs and
  unrelated `OSError`s keep answering `500`. `/health/ready` is unchanged.
- **Audit reasons follow the answer.** A posting that fails this way records `failed` /
  `temporarily_unavailable` (with `connection_invalidated` for a lost connection). Other audited
  services record the same reason for an acquisition failure, when the event can be written.
- **A posting can outlive its request.** After a client disconnect, a cancellation, or a kill
  during COMMIT, the posting may be committed while the client never heard. A retry with the
  **same** `Idempotency-Key` is how the client learns what happened: a replay of the committed
  posting, or a fresh attempt (which may itself succeed, be rejected, fail, or wait and answer
  `503` while the original is still finishing). Either way the posting is applied at most once
  per key.
- **Limitations, accepted:**
  - A connection lost mid-transaction outside postings answers `500`.
  - Transient refusals the server itself reports at connect time answer `500`.
  - Under pool exhaustion a posting can take up to about twice the pool timeout (30 s by
    default) to answer: once for the posting, once more for its failure-audit write, which then
    logs as lost.
  - **A stalled network** (a COMMIT, or any statement, whose reply never arrives) blocks the
    request and its pooled connection until the connection fails, after a time set by the
    operating system's TCP settings: the server-side timeouts do not apply to an idle server,
    and cancelling the request does not free it (observed with a proxy withholding COMMIT's
    reply). No client-side bound exists yet (see the deferred alternative).
  - The audit writer's non-cancellation path (an `Exception` from the event's COMMIT, e.g. a
    lost connection) still logs "not written", although that outcome is also uncertain.
  - The classification relies on the driver behaviour listed in Context; a library upgrade that
    changes it fails the tests rather than silently changing the answers.
- **Tests:** `test_database_availability.py` (classification), `test_database_unavailability.py`
  and `test_database_unavailability_api.py` (outages, pool timeout, pre-COMMIT loss, audit
  reasons), `test_posting_cancellation.py` (cancellation before and after COMMIT, in a lock
  wait, during audit writes, of a replay; `tests/tcp_proxy.py` withholds COMMIT
  acknowledgements). The COMMIT-time tests of `test_commit_outcome.py` are unchanged.

## Amendment (Phase 10, 2026-10-23): a logging handler (ADR 0017)

Logging is configured: every record, the fallback lines of section 4 included, is one JSON line
on stderr with the request's id, and those fallback lines also name the event's `request_id`.
They remain a fallback, as durable as stderr. See
[ADR 0017](0017-structured-logs-and-request-correlation.md).
