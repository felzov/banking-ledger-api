from sqlalchemy.exc import DBAPIError, IntegrityError


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
