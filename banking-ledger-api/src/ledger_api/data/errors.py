from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError


class DatabaseUnavailableError(ConnectionError):
    """A new database connection could not be established: refused, unreachable, timed out.

    Raised only by the engine's connect hook (data.engine), from the OSError the driver raised
    while connecting; the OSError is its __cause__. No statement was sent on that connection,
    so nothing the caller attempted can have happened. A ConnectionError, so code that catches
    OSError (the readiness probe) still does. A refusal the server itself reported (unknown
    database, bad credentials) is a DBAPIError instead, and is not this.
    """


class PrivilegedDatabaseRoleError(DatabaseUnavailableError):
    """The database role the application connected as is too powerful to serve requests with
    (Phase 10, ADR 0015), or its privileges could not be verified: the connection is discarded.

    A DatabaseUnavailableError, so every request that needs the database is answered like an
    unreachable database (503 temporarily_unavailable, a generic body) and the readiness probe
    reports unavailable. The reason is logged at CRITICAL, never sent to the client. The message
    names no host, user or other connection parameter.
    """


def violated_constraint(error: IntegrityError) -> str | None:
    """Name of the constraint PostgreSQL reported, or None if it reported none.

    The DBAPI error wraps asyncpg's exception, which carries the constraint name. Errors
    raised by the ledger triggers set it too, via RAISE ... USING CONSTRAINT. Services
    translate violations by exact name only: any other IntegrityError is a bug and must
    surface as one.
    """
    cause = error.orig.__cause__ if error.orig is not None else None
    name = getattr(cause, "constraint_name", None)
    return name if isinstance(name, str) else None


def sqlstate(error: DBAPIError) -> str | None:
    """The SQLSTATE PostgreSQL reported (e.g. 40P01 deadlock_detected), or None."""
    code = getattr(error.orig, "sqlstate", None)
    return code if isinstance(code, str) else None


def server_severity(error: DBAPIError) -> str | None:
    """The severity of the error message PostgreSQL itself sent (ERROR, FATAL, PANIC), or None
    if the error was raised on the client side: a closed or broken connection, a driver failure.

    Reads the non-localized field (severity_en). A SQLSTATE alone cannot tell the two apart:
    asyncpg's client-side connection errors carry a class-level SQLSTATE (a connection closed
    mid-COMMIT is ConnectionDoesNotExistError, "08003") without any server fields.
    """
    cause = error.orig.__cause__ if error.orig is not None else None
    severity = getattr(cause, "severity_en", None)
    return severity if isinstance(severity, str) else None


def is_acquisition_failure(error: BaseException) -> bool:
    """Whether getting a connection failed: none could be established, or the pool had none
    free within its timeout. The transaction then never sent a statement, whatever it was."""
    return isinstance(error, DatabaseUnavailableError | PoolTimeoutError)


def is_connection_lost(error: BaseException) -> bool:
    """Whether an open connection died on the client side: invalidated, without any error
    message from PostgreSQL itself (a reset, a closed socket, a terminated backend).

    The error alone cannot tell WHEN: a connection lost before COMMIT and one lost during it
    raise the same error. Only a caller that knows COMMIT had not been sent may conclude that
    nothing was committed (services.posting does).
    """
    return (
        isinstance(error, DBAPIError)
        and error.connection_invalidated
        and server_severity(error) is None
    )
