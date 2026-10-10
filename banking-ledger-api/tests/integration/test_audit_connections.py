"""One audited operation never holds two database connections (ADR 0011).

Every test runs on an engine whose pool has exactly ONE connection and gives up after
POOL_TIMEOUT seconds. If an audit transaction needed a second connection while the business
transaction still held the first, it would time out, the event would be missing and an ERROR
would be logged. Each operation here must leave its event, log nothing, and return the
connection to the pool.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import URL, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import QueuePool

from ledger_api.data import posting as data_posting
from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.models import AuditEvent, Transaction
from ledger_api.domain.audit import AuditAction, AuditOutcome, AuditRecord
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import InsufficientFundsError, UserNotFoundError
from ledger_api.services import audit as audit_service
from ledger_api.services import posting as posting_service
from ledger_api.services.accounts import open_customer_account
from ledger_api.services.posting import withdraw
from tests.database import audit_events_about
from tests.factories import FundedAccount, commit_funded_account

POOL_TIMEOUT = 1
AUDIT_LOGGER = "ledger_api.services.audit"


class InjectedFailureError(Exception):
    pass


@pytest.fixture
async def single_connection_engine(
    engine: AsyncEngine, test_database_url: URL
) -> AsyncIterator[AsyncEngine]:
    # Depends on `engine` so the test database exists and is migrated.
    single = create_async_engine(
        test_database_url, pool_size=1, max_overflow=0, pool_timeout=POOL_TIMEOUT
    )
    try:
        yield single
    finally:
        await single.dispose()


@pytest.fixture
def sessionmaker(single_connection_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(single_connection_engine)


@pytest.fixture
async def funded(sessionmaker: async_sessionmaker[AsyncSession]) -> FundedAccount:
    return await commit_funded_account(sessionmaker, 5_000)


def _checked_out(engine: AsyncEngine) -> int:
    pool = engine.pool
    assert isinstance(pool, QueuePool)  # checkedout() is QueuePool API
    return pool.checkedout()


async def _latest_event(
    sessionmaker: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> tuple[AuditOutcome, str | None]:
    async with sessionmaker() as session:
        *_, event = await audit_events_about(session, account_id)
    return event.outcome, event.reason


async def test_success_on_a_single_connection(
    sessionmaker: async_sessionmaker[AsyncSession],
    single_connection_engine: AsyncEngine,
    funded: FundedAccount,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER):
        async with sessionmaker() as session:
            await withdraw(
                session, owner_id=funded.owner_id, account_id=funded.account_id, amount_minor=1
            )

    assert await _latest_event(sessionmaker, funded.account_id) == (AuditOutcome.SUCCEEDED, None)
    assert caplog.records == []
    assert _checked_out(single_connection_engine) == 0


async def test_rejection_on_a_single_connection(
    sessionmaker: async_sessionmaker[AsyncSession],
    single_connection_engine: AsyncEngine,
    funded: FundedAccount,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER):
        async with sessionmaker() as session:
            with pytest.raises(InsufficientFundsError):
                await withdraw(
                    session,
                    owner_id=funded.owner_id,
                    account_id=funded.account_id,
                    amount_minor=10_000,
                )

    assert await _latest_event(sessionmaker, funded.account_id) == (
        AuditOutcome.REJECTED,
        "insufficient_funds",
    )
    assert caplog.records == []
    assert _checked_out(single_connection_engine) == 0


async def test_injected_failure_on_a_single_connection(
    sessionmaker: async_sessionmaker[AsyncSession],
    single_connection_engine: AsyncEngine,
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_balance_update(*args: Any, **kwargs: Any) -> dict[uuid.UUID, int]:
        raise InjectedFailureError

    monkeypatch.setattr(posting_service, "apply_balance_deltas", fail_balance_update)

    with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER):
        async with sessionmaker() as session:
            with pytest.raises(InjectedFailureError):
                await withdraw(
                    session, owner_id=funded.owner_id, account_id=funded.account_id, amount_minor=1
                )

    assert await _latest_event(sessionmaker, funded.account_id) == (
        AuditOutcome.FAILED,
        "internal_error",
    )
    assert caplog.records == []
    assert _checked_out(single_connection_engine) == 0


async def test_commit_time_failure_on_a_single_connection(
    sessionmaker: async_sessionmaker[AsyncSession],
    single_connection_engine: AsyncEngine,
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def miscounted(**values: Any) -> Transaction:
        return Transaction(**{**values, "entry_count": values["entry_count"] + 1})

    monkeypatch.setattr(data_posting, "Transaction", miscounted)

    with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER):
        async with sessionmaker() as session:
            with pytest.raises(IntegrityError):
                await withdraw(
                    session, owner_id=funded.owner_id, account_id=funded.account_id, amount_minor=1
                )

    assert await _latest_event(sessionmaker, funded.account_id) == (
        AuditOutcome.FAILED,
        "internal_error",
    )
    assert caplog.records == []
    assert _checked_out(single_connection_engine) == 0


async def test_account_rejection_on_a_single_connection(
    sessionmaker: async_sessionmaker[AsyncSession],
    single_connection_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stranger = uuid.uuid7()

    with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER):
        async with sessionmaker() as session:
            with pytest.raises(UserNotFoundError):
                await open_customer_account(session, owner_id=stranger, currency=Currency.GBP)

    async with sessionmaker() as session:
        event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.details["requested_user_id"].astext == str(stranger)
            )
        )
    assert event is not None
    assert (event.outcome, event.reason) == (AuditOutcome.REJECTED, "user_not_found")
    assert caplog.records == []
    assert _checked_out(single_connection_engine) == 0


async def test_the_single_connection_pool_does_detect_a_second_connection(
    sessionmaker: async_sessionmaker[AsyncSession],
    single_connection_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Control: proves the tests above would fail if two connections were needed. While one
    # session holds the only connection, an audit write on another session cannot get one;
    # the writer gives up after the pool timeout, logs, and still does not raise.
    record = AuditRecord(AuditAction.USER_REGISTER, AuditOutcome.REJECTED, reason="probe")

    async with sessionmaker() as holder, holder.begin(), sessionmaker() as other:
        await holder.connection()  # check out the pool's only connection
        with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER):
            await audit_service.record_outside_transaction(other, record)

    [log] = caplog.records
    assert "TimeoutError" in log.getMessage()
