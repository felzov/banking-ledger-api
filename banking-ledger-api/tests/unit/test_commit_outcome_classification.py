"""Which COMMIT failures prove a rollback, and which leave the outcome unknown (ADR 0006).

Every error is built the way the application receives it: an asyncpg exception (from server
fields, exactly as asyncpg parses an ErrorResponse, or as asyncpg raises it client-side),
translated by SQLAlchemy's asyncpg adapter into its DBAPI error (the asyncpg exception is the
adapter error's __cause__), wrapped by DBAPIError.instance with the connection state. The
shapes match what tests/integration/test_commit_outcome.py observes against a real server.
"""

import asyncpg  # type: ignore[import-untyped]  # asyncpg ships no type information
import pytest
from asyncpg.exceptions import ConnectionDoesNotExistError  # type: ignore[import-untyped]
from asyncpg.exceptions._base import PostgresError  # type: ignore[import-untyped]
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import DBAPIError, IntegrityError

from ledger_api.data.errors import server_severity, sqlstate
from ledger_api.services.posting import _definitely_rolled_back

DBAPI = AsyncAdapt_asyncpg_dbapi(asyncpg)  # type: ignore[no-untyped-call]


def _as_sqlalchemy_error(driver_error: Exception, *, invalidated: bool = False) -> DBAPIError:
    """driver_error as SQLAlchemy raises it from COMMIT."""
    translated = None
    for cls in type(driver_error).__mro__:  # the adapter's own lookup
        if cls in DBAPI._asyncpg_error_translate:
            translated = DBAPI._asyncpg_error_translate[cls](str(driver_error), driver_error)
            break
    assert translated is not None
    translated.__cause__ = driver_error  # the adapter raises it "from" the asyncpg error
    wrapped = DBAPIError.instance(
        "COMMIT", None, translated, DBAPI.Error, connection_invalidated=invalidated
    )
    assert isinstance(wrapped, DBAPIError)
    return wrapped


def _server_error(code: str, severity: str = "ERROR") -> PostgresError:
    """An error PostgreSQL sent: asyncpg builds it from the ErrorResponse fields."""
    error = PostgresError.new({"S": severity, "V": severity, "C": code, "M": "server message"})
    assert isinstance(error, PostgresError)
    return error


# A non-fatal ERROR from the server, on a live connection: the server aborted the transaction.
DEFINITE = [
    "23514",  # check_violation: the deferred double-entry checks
    "23503",  # foreign_key_violation: the deferred key -> transaction reference
    "40P01",  # deadlock_detected
    "40001",  # serialization_failure
    "55P03",  # lock_not_available (lock_timeout)
    "57014",  # query_canceled (statement_timeout)
    "42501",  # insufficient_privilege: a deferred check run by a mis-granted role
    "P0001",  # raise_exception: a trigger RAISE without an ERRCODE
    "22003",  # numeric_value_out_of_range
    "53100",  # disk_full
    "XX000",  # internal_error
]


@pytest.mark.parametrize("code", DEFINITE)
def test_a_server_error_on_a_live_connection_is_a_definite_rollback(code: str) -> None:
    error = _as_sqlalchemy_error(_server_error(code))

    assert (sqlstate(error), server_severity(error), error.connection_invalidated) == (
        code,
        "ERROR",
        False,
    )
    assert _definitely_rolled_back(error)


@pytest.mark.parametrize(
    "code",
    [
        "40003",  # statement_completion_unknown: the code itself says the outcome is unknown
        "08007",  # transaction_resolution_unknown
        "08006",  # connection_failure, even when a server sends it as an ERROR
        "08000",  # connection_exception
    ],
)
def test_outcome_unknown_sqlstates_are_never_a_definite_rollback(code: str) -> None:
    error = _as_sqlalchemy_error(_server_error(code))

    assert (server_severity(error), error.connection_invalidated) == ("ERROR", False)
    assert not _definitely_rolled_back(error)


@pytest.mark.parametrize("code", ["57P01", "57P02", "XX000"])
@pytest.mark.parametrize("severity", ["FATAL", "PANIC"])
def test_a_fatal_or_panic_from_the_server_is_unknown(code: str, severity: str) -> None:
    # The session ended; a termination can be reported after the commit record was written.
    error = _as_sqlalchemy_error(_server_error(code, severity))

    assert server_severity(error) == severity
    assert not _definitely_rolled_back(error)


@pytest.mark.parametrize("code", ["23514", "40P01", "42501", "XX000"])
def test_any_error_on_an_invalidated_connection_is_unknown(code: str) -> None:
    error = _as_sqlalchemy_error(_server_error(code), invalidated=True)

    assert error.connection_invalidated
    assert not _definitely_rolled_back(error)


def test_a_connection_closed_during_commit_is_unknown_despite_its_class_level_sqlstate() -> None:
    # What the driver raised when the backend was terminated in the middle of COMMIT (observed
    # in the integration test): a client-side asyncpg error whose CLASS carries SQLSTATE 08003.
    driver_error = ConnectionDoesNotExistError("connection was closed in the middle of operation")

    for invalidated in (True, False):  # even if the connection were not flagged
        error = _as_sqlalchemy_error(driver_error, invalidated=invalidated)
        assert sqlstate(error) == "08003"
        assert server_severity(error) is None  # never sent by a server
        assert not _definitely_rolled_back(error)


def test_a_driver_error_without_any_sqlstate_is_unknown() -> None:
    # What the driver raised when the backend was terminated before COMMIT was sent.
    error = _as_sqlalchemy_error(asyncpg.InterfaceError("connection is closed"), invalidated=True)

    assert (sqlstate(error), server_severity(error)) == (None, None)
    assert not _definitely_rolled_back(error)


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("reset by peer"),
        TimeoutError(),
        RuntimeError("unexpected"),
    ],
    ids=["connection-reset", "timeout", "other"],
)
def test_errors_that_are_not_database_errors_are_unknown(error: Exception) -> None:
    assert not _definitely_rolled_back(error)


def test_a_fake_sqlstate_without_server_fields_is_unknown() -> None:
    # A DBAPIError whose driver error only claims a SQLSTATE (no server severity), like the
    # stand-ins some older tests use: it proves nothing about the server.
    class _DriverError(Exception):
        sqlstate = "23514"

    error = DBAPIError("COMMIT", None, _DriverError())

    assert sqlstate(error) == "23514"
    assert server_severity(error) is None
    assert not _definitely_rolled_back(error)


def test_the_integrity_errors_of_the_deferred_checks_keep_their_type() -> None:
    # Classified as definite, the existing handling still sees an IntegrityError (500).
    error = _as_sqlalchemy_error(_server_error("23514"))

    assert isinstance(error, IntegrityError)
    assert _definitely_rolled_back(error)
