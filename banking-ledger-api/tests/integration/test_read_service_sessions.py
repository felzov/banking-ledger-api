"""Read services own their transaction, like write services (Phase 7).

Every read service must return with the session outside any transaction and its connection
back in the pool, whether it succeeds or raises. Before Phase 7 they relied on SQLAlchemy's
autobegin and left a transaction open: a write service called next on the same session then
failed with "A transaction is already begun", and its failure audit was lost.

Runs on an engine whose pool has exactly ONE connection, with real COMMITs: a service that
kept its connection would show it checked out, and the next call would time out.
"""

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import QueuePool

from ledger_api.data.engine import create_sessionmaker
from ledger_api.domain.audit import AuditOutcome
from ledger_api.domain.errors import (
    AccountNotFoundError,
    InsufficientFundsError,
    InvalidStatementCursorError,
    UserNotFoundError,
)
from ledger_api.services.accounts import get_customer_account, list_customer_accounts
from ledger_api.services.posting import deposit, withdraw
from ledger_api.services.statements import get_statement
from ledger_api.services.users import get_user
from tests.database import audit_events_about
from tests.factories import FundedAccount, commit_funded_account

POOL_TIMEOUT = 1
AUDIT_LOGGER = "ledger_api.services.audit"

type Maker = async_sessionmaker[AsyncSession]
type Read = Callable[[AsyncSession, FundedAccount], Awaitable[Any]]


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
def sessionmaker(single_connection_engine: AsyncEngine) -> Maker:
    return create_sessionmaker(single_connection_engine)


@pytest.fixture
async def funded(sessionmaker: Maker) -> FundedAccount:
    return await commit_funded_account(sessionmaker, 1_000)


def _checked_out(engine: AsyncEngine) -> int:
    pool = engine.pool
    assert isinstance(pool, QueuePool)
    return pool.checkedout()


READS: dict[str, Read] = {
    "get_user": lambda session, account: get_user(session, user_id=account.owner_id),
    "get_customer_account": lambda session, account: get_customer_account(
        session, owner_id=account.owner_id, account_id=account.account_id
    ),
    "list_customer_accounts": lambda session, account: list_customer_accounts(
        session, owner_id=account.owner_id
    ),
    "get_statement": lambda session, account: get_statement(
        session, owner_id=account.owner_id, account_id=account.account_id
    ),
}

FAILING_READS: dict[str, tuple[Read, type[Exception]]] = {
    "get_user/unknown": (
        lambda session, _: get_user(session, user_id=uuid.uuid7()),
        UserNotFoundError,
    ),
    "get_customer_account/foreign": (
        lambda session, account: get_customer_account(
            session, owner_id=uuid.uuid7(), account_id=account.account_id
        ),
        AccountNotFoundError,
    ),
    "list_customer_accounts/unknown": (
        lambda session, _: list_customer_accounts(session, owner_id=uuid.uuid7()),
        UserNotFoundError,
    ),
    "get_statement/foreign": (
        lambda session, account: get_statement(
            session, owner_id=uuid.uuid7(), account_id=account.account_id
        ),
        AccountNotFoundError,
    ),
    "get_statement/bad-cursor": (
        lambda session, account: get_statement(
            session,
            owner_id=account.owner_id,
            account_id=account.account_id,
            cursor=uuid.uuid7(),
        ),
        InvalidStatementCursorError,
    ),
}


@pytest.mark.parametrize("name", READS)
async def test_a_read_returns_the_session_outside_a_transaction(
    sessionmaker: Maker,
    single_connection_engine: AsyncEngine,
    funded: FundedAccount,
    name: str,
) -> None:
    async with sessionmaker() as session:
        await READS[name](session, funded)

        # Checked while the session is still open: nothing it holds stays behind.
        assert not session.in_transaction()
        assert _checked_out(single_connection_engine) == 0


@pytest.mark.parametrize("name", FAILING_READS)
async def test_a_failing_read_returns_the_session_outside_a_transaction(
    sessionmaker: Maker,
    single_connection_engine: AsyncEngine,
    funded: FundedAccount,
    name: str,
) -> None:
    read, error = FAILING_READS[name]

    async with sessionmaker() as session:
        with pytest.raises(error):
            await read(session, funded)

        assert not session.in_transaction()
        assert _checked_out(single_connection_engine) == 0


@pytest.mark.parametrize("name", READS)
async def test_a_write_after_a_read_on_the_same_session_posts_and_audits(
    sessionmaker: Maker,
    single_connection_engine: AsyncEngine,
    funded: FundedAccount,
    caplog: pytest.LogCaptureFixture,
    name: str,
) -> None:
    key = f"after-{name}-{uuid.uuid7().hex}"

    with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER):
        async with sessionmaker() as session:
            await READS[name](session, funded)
            posted = await deposit(
                session,
                owner_id=funded.owner_id,
                account_id=funded.account_id,
                amount_minor=250,
                idempotency_key=key,
            )
            assert not session.in_transaction()

    assert posted.balances == {funded.account_id: 1_250}
    assert caplog.records == []  # the audit writer never had to refuse
    async with sessionmaker() as session:
        *_, event = await audit_events_about(session, funded.account_id)
    assert (event.outcome, event.transaction_id, event.idempotency_key) == (
        AuditOutcome.SUCCEEDED,
        posted.transaction_id,
        key,
    )
    assert _checked_out(single_connection_engine) == 0


@pytest.mark.parametrize("name", READS)
async def test_a_rejected_write_after_a_read_is_still_audited(
    sessionmaker: Maker,
    single_connection_engine: AsyncEngine,
    funded: FundedAccount,
    caplog: pytest.LogCaptureFixture,
    name: str,
) -> None:
    # The path that used to lose its audit event: the rejection is recorded after the
    # rollback, which requires the session to be outside a transaction.
    with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER):
        async with sessionmaker() as session:
            await READS[name](session, funded)
            with pytest.raises(InsufficientFundsError):
                await withdraw(
                    session,
                    owner_id=funded.owner_id,
                    account_id=funded.account_id,
                    amount_minor=1_001,
                )

    assert caplog.records == []
    async with sessionmaker() as session:
        *_, event = await audit_events_about(session, funded.account_id)
    assert (event.outcome, event.reason) == (AuditOutcome.REJECTED, "insufficient_funds")
    assert _checked_out(single_connection_engine) == 0


async def test_reads_can_be_chained_on_one_session(
    sessionmaker: Maker, single_connection_engine: AsyncEngine, funded: FundedAccount
) -> None:
    # Each read begins and ends its own transaction, so none of them meets another's.
    async with sessionmaker() as session:
        for read in READS.values():
            await read(session, funded)
        assert not session.in_transaction()

    assert _checked_out(single_connection_engine) == 0
