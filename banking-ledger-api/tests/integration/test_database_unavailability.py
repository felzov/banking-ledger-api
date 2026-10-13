"""Database connections that cannot be obtained, or are lost before COMMIT (Phase 9 A3).

Nothing can have been committed in either case, so a posting reports temporarily_unavailable
(503) and the client may retry. A connection lost DURING COMMIT is different and stays
outcome_unknown: test_commit_outcome.py pins that, unchanged.

Connection failures go through the application's own engine (data.engine.create_engine): only
its connect hook tells "could not connect" apart from any other OSError.
"""

import logging
import uuid
from collections.abc import Collection

import pytest
from sqlalchemy import URL, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from ledger_api.data.engine import create_engine, create_sessionmaker
from ledger_api.data.errors import (
    DatabaseUnavailableError,
    is_acquisition_failure,
    server_severity,
    sqlstate,
)
from ledger_api.data.models import AuditEvent
from ledger_api.data.posting import LockedAccount, lock_accounts
from ledger_api.domain.audit import AuditAction, AuditOutcome
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import TemporarilyUnavailableError
from ledger_api.services import accounts as accounts_service
from ledger_api.services import posting as posting_service
from ledger_api.services.accounts import open_customer_account
from ledger_api.services.posting import withdraw
from ledger_api.services.users import register_user
from tests.conftest import UNREACHABLE_DATABASE_URL, settings_for
from tests.database import configured_database_url, disposable_database_url
from tests.factories import FundedAccount, commit_funded_account
from tests.integration.test_commit_outcome import (
    AMOUNT,
    FUNDED,
    Maker,
    _assert_reconciled,
    _balance,
    _key_row,
    _withdraw,
    _withdrawal_events,
)

AUDIT_LOGGER = "ledger_api.services.audit"


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> Maker:
    return create_sessionmaker(engine)


@pytest.fixture
async def funded(sessionmaker: Maker) -> FundedAccount:
    return await commit_funded_account(sessionmaker, FUNDED)


def _never_created_database() -> URL:
    # A disposable name that no test creates: the server itself refuses the connection.
    return disposable_database_url(configured_database_url(), "_never_created")


def _lost_audit_events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == AUDIT_LOGGER and record.getMessage().startswith("audit event not written")
    ]


# --- connecting ----------------------------------------------------------------------------------


async def test_an_unreachable_database_raises_database_unavailable() -> None:
    engine = create_engine(settings_for(UNREACHABLE_DATABASE_URL))
    try:
        with pytest.raises(DatabaseUnavailableError) as excinfo:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()

    assert isinstance(excinfo.value.__cause__, ConnectionRefusedError)
    assert is_acquisition_failure(excinfo.value)


async def test_a_connection_refused_by_postgresql_itself_is_not_database_unavailable() -> None:
    engine = create_engine(settings_for(_never_created_database()))
    try:
        with pytest.raises(DBAPIError) as excinfo:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()

    # The server answered (invalid_catalog_name): a misconfiguration, still internal_error.
    assert (sqlstate(excinfo.value), server_severity(excinfo.value)) == ("3D000", "FATAL")
    assert not isinstance(excinfo.value, DatabaseUnavailableError)
    assert not is_acquisition_failure(excinfo.value)


# --- postings ------------------------------------------------------------------------------------


async def test_a_posting_on_an_unreachable_database_is_temporarily_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_engine(settings_for(UNREACHABLE_DATABASE_URL))
    try:
        async with create_sessionmaker(engine)() as session:
            with (
                caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER),
                pytest.raises(TemporarilyUnavailableError) as excinfo,
            ):
                await withdraw(
                    session,
                    owner_id=uuid.uuid7(),
                    account_id=uuid.uuid7(),
                    amount_minor=AMOUNT,
                    idempotency_key=f"unreachable-{uuid.uuid7().hex}",
                )
    finally:
        await engine.dispose()

    assert isinstance(excinfo.value.__cause__, DatabaseUnavailableError)
    # The failure event could not be written either: logged as lost, never claimed as recorded.
    [lost] = _lost_audit_events(caplog)
    assert "reason=temporarily_unavailable" in lost


async def test_a_posting_that_gets_no_pooled_connection_is_temporarily_unavailable(
    test_database_url: URL,
    sessionmaker: Maker,
    funded: FundedAccount,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = f"pool-timeout-{uuid.uuid7().hex}"
    single = create_async_engine(test_database_url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    try:
        async with single.connect() as held:
            await held.execute(text("SELECT 1"))  # the pool's only connection is in use
            with (
                caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER),
                pytest.raises(TemporarilyUnavailableError) as excinfo,
            ):
                await _withdraw(create_sessionmaker(single), funded, key)
    finally:
        await single.dispose()

    assert isinstance(excinfo.value.__cause__, PoolTimeoutError)
    # The failure event needed a connection from the same exhausted pool: lost, and logged.
    [lost] = _lost_audit_events(caplog)
    assert lost.startswith("audit event not written (TimeoutError)")
    assert "reason=temporarily_unavailable" in lost
    # Nothing was posted, and the key was never consumed.
    assert await _key_row(sessionmaker, funded, key) is None
    assert await _balance(sessionmaker, funded) == FUNDED
    assert await _withdrawal_events(sessionmaker, funded) == []


async def test_a_connection_lost_before_commit_is_temporarily_unavailable_and_the_retry_runs_once(
    sessionmaker: Maker,
    engine: AsyncEngine,
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real connection loss through the real driver, BEFORE COMMIT: the backend is terminated
    # after the key claim (the posting's first write) and before the account locks. PostgreSQL
    # rolls back the open transaction of a terminated backend, and COMMIT was never sent, so
    # unlike a loss during COMMIT (test_commit_outcome.py) the outcome is known: nothing.
    key = f"lost-before-commit-{uuid.uuid7().hex}"
    real_lock = lock_accounts
    terminated: list[int] = []

    async def lose_the_connection_then_lock(
        session: AsyncSession, account_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, LockedAccount]:
        if not terminated:
            pid = await session.scalar(select(func.pg_backend_pid()))
            assert isinstance(pid, int)
            terminated.append(pid)
            async with engine.connect() as admin:
                terminate = text("SELECT pg_terminate_backend(:pid, 5000)")
                assert await admin.scalar(terminate, {"pid": pid})
        return await real_lock(session, account_ids)

    monkeypatch.setattr(posting_service, "lock_accounts", lose_the_connection_then_lock)

    with pytest.raises(TemporarilyUnavailableError) as excinfo:
        await _withdraw(sessionmaker, funded, key)

    assert terminated
    cause = excinfo.value.__cause__
    assert isinstance(cause, DBAPIError)
    assert cause.connection_invalidated  # the driver saw the connection die
    assert server_severity(cause) is None  # on the client side: no answer from PostgreSQL
    # The persisted truth: nothing committed, the claim included.
    assert await _key_row(sessionmaker, funded, key) is None
    assert await _balance(sessionmaker, funded) == FUNDED
    [failed] = await _withdrawal_events(sessionmaker, funded)
    assert (failed.outcome, failed.reason) == (AuditOutcome.FAILED, "temporarily_unavailable")
    assert failed.idempotency_key == key
    assert failed.details["connection_invalidated"] is True
    assert "attempted_transaction_id" not in failed.details  # lost before the header existed

    retried = await _withdraw(sessionmaker, funded, key)

    assert not retried.replayed  # nothing to replay: it runs now
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT  # exactly once
    row = await _key_row(sessionmaker, funded, key)
    assert row is not None
    assert row.transaction_id == retried.transaction_id
    await _assert_reconciled(sessionmaker)


# --- other services ------------------------------------------------------------------------------


async def test_an_acquisition_failure_outside_postings_is_audited_when_the_event_can_be_written(
    sessionmaker: Maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Injected: a real outage would also fail the audit write (see the tests above). The
    # business transaction cannot get a connection; the audit transaction then can.
    async with sessionmaker() as session:
        user = await register_user(session, email=f"a3-{uuid.uuid7().hex}@example.com")

    async def unreachable(*args: object, **kwargs: object) -> None:
        raise DatabaseUnavailableError("could not connect to the database") from (
            ConnectionRefusedError(111, "Connect call failed")
        )

    monkeypatch.setattr(accounts_service, "get_ledger_by_currency", unreachable)

    async with sessionmaker() as session:
        with pytest.raises(DatabaseUnavailableError):  # the API answers it with 503
            await open_customer_account(session, owner_id=user.id, currency=Currency.GBP)

    async with sessionmaker() as session:
        [event] = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.ACCOUNT_OPEN,
                    AuditEvent.actor_user_id == user.id,
                )
            )
        ).all()
    assert (event.outcome, event.reason) == (AuditOutcome.FAILED, "temporarily_unavailable")
    assert event.details["error_type"] == "DatabaseUnavailableError"
