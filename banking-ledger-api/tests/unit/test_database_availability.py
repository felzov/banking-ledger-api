"""Which database failures mean "no connection could be obtained" or "the connection was lost"
(Phase 9 A3), and the audit reason a caller-visible acquisition failure is recorded with.

Errors are built as the application receives them (see test_commit_outcome_classification):
a server error carries PostgreSQL's severity; a client-side loss carries none.
"""

import uuid
from collections.abc import Callable

import asyncpg  # type: ignore[import-untyped]  # asyncpg ships no type information
import pytest
from asyncpg.exceptions import ConnectionDoesNotExistError  # type: ignore[import-untyped]
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from ledger_api.data.errors import (
    DatabaseUnavailableError,
    is_acquisition_failure,
    is_connection_lost,
)
from ledger_api.domain.audit import AuditAction, AuditOutcome
from ledger_api.services.audit import AuditContext
from tests.unit.test_commit_outcome_classification import _as_sqlalchemy_error, _server_error


def _unavailable() -> DatabaseUnavailableError:
    error = DatabaseUnavailableError("could not connect to the database")
    error.__cause__ = ConnectionRefusedError(111, "Connect call failed")
    return error


def _client_side_loss(*, invalidated: bool = True) -> DBAPIError:
    # Observed when a backend is terminated, or the socket is reset, before COMMIT is sent.
    return _as_sqlalchemy_error(
        asyncpg.InterfaceError("connection is closed"), invalidated=invalidated
    )


def _closed_mid_operation(*, invalidated: bool = True) -> DBAPIError:
    # Observed for a reset or terminated connection mid-statement, and during COMMIT.
    driver_error = ConnectionDoesNotExistError("connection was closed in the middle of operation")
    return _as_sqlalchemy_error(driver_error, invalidated=invalidated)


ACQUISITION_FAILURES = {
    "unreachable": _unavailable,
    "pool-timeout": lambda: PoolTimeoutError("QueuePool limit of size 1 overflow 0 reached"),
}

NOT_ACQUISITION_FAILURES = {
    # Raw OSErrors are someone else's: only the engine's connect hook knows one came from
    # connecting, and it re-raises it as DatabaseUnavailableError.
    "raw-connection-refused": lambda: ConnectionRefusedError(111, "refused"),
    "builtin-timeout": TimeoutError,
    "file-not-found": lambda: FileNotFoundError("/nonexistent"),
    "client-side-loss": _client_side_loss,
    "closed-mid-operation": _closed_mid_operation,
    # The server answered: misconfiguration (unknown database, bad credentials), not a failure
    # to reach it.
    "unknown-database-fatal": lambda: _as_sqlalchemy_error(_server_error("3D000", "FATAL")),
    "bad-password-fatal": lambda: _as_sqlalchemy_error(_server_error("28P01", "FATAL")),
    "check-violation": lambda: _as_sqlalchemy_error(_server_error("23514")),
    "programming-error": lambda: ValueError("a bug"),
}


@pytest.mark.parametrize("make", ACQUISITION_FAILURES.values(), ids=ACQUISITION_FAILURES)
def test_connection_acquisition_failures(make: Callable[[], Exception]) -> None:
    error = make()

    assert is_acquisition_failure(error)
    assert not is_connection_lost(error)


@pytest.mark.parametrize("make", NOT_ACQUISITION_FAILURES.values(), ids=NOT_ACQUISITION_FAILURES)
def test_everything_else_is_not_an_acquisition_failure(make: Callable[[], Exception]) -> None:
    assert not is_acquisition_failure(make())


@pytest.mark.parametrize("make", [_client_side_loss, _closed_mid_operation])
def test_a_client_side_loss_on_an_invalidated_connection_is_a_lost_connection(
    make: Callable[[], DBAPIError],
) -> None:
    assert is_connection_lost(make())


@pytest.mark.parametrize(
    "error",
    [
        _client_side_loss(invalidated=False),  # SQLAlchemy did not see the connection die
        _closed_mid_operation(invalidated=False),
        # PostgreSQL itself reported the error, even on an invalidated connection: not a loss.
        _as_sqlalchemy_error(_server_error("57P01", "FATAL"), invalidated=True),
        _as_sqlalchemy_error(_server_error("23514"), invalidated=True),
        _as_sqlalchemy_error(_server_error("23514")),
        ConnectionResetError("reset by peer"),  # raw, not through SQLAlchemy
        _unavailable(),  # never connected: an acquisition failure, not a loss
    ],
    ids=[
        "client-side-not-invalidated",
        "closed-not-invalidated",
        "server-fatal-invalidated",
        "server-error-invalidated",
        "server-error",
        "raw-reset",
        "unavailable",
    ],
)
def test_other_errors_are_not_a_lost_connection(error: Exception) -> None:
    assert not is_connection_lost(error)


def test_an_unavailable_database_is_still_an_oserror() -> None:
    # The readiness probe catches OSError: it must keep answering 503, not crash.
    assert isinstance(_unavailable(), OSError)


def _failed_reason(error: Exception) -> str | None:
    context = AuditContext(AuditAction.ACCOUNT_OPEN, uuid.uuid7(), {"currency": "GBP"})
    record = context.failed(error)
    assert record.outcome == AuditOutcome.FAILED
    return record.reason


@pytest.mark.parametrize("make", ACQUISITION_FAILURES.values(), ids=ACQUISITION_FAILURES)
def test_an_acquisition_failure_is_audited_as_what_the_caller_is_told(
    make: Callable[[], Exception],
) -> None:
    assert _failed_reason(make()) == "temporarily_unavailable"


@pytest.mark.parametrize(
    "error",
    [_client_side_loss(), FileNotFoundError("/nonexistent"), ValueError("a bug")],
    ids=["client-side-loss", "file-not-found", "programming-error"],
)
def test_other_failures_are_still_audited_as_internal_errors(error: Exception) -> None:
    # Outside postings, a connection lost mid-transaction still answers 500 (A3 scope).
    assert _failed_reason(error) == "internal_error"
