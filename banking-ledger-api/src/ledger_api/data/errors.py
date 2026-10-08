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
