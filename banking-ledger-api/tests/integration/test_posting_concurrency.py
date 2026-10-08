"""Concurrent postings on real, separate connections with real COMMITs.

Retries are DISABLED in this module (MAX_ATTEMPTS = 1): a deadlock surfaces as a failure
instead of being retried away, so these tests prove that the lock order alone (one sorted
FOR UPDATE OF accounts) keeps concurrent postings correct and deadlock-free.
"""

import asyncio
import random
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.models import Account, LedgerEntry, Transaction
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import InsufficientFundsError
from ledger_api.domain.transaction import TransactionKind
from ledger_api.services import posting as posting_service
from ledger_api.services.posting import PostingResult, deposit, transfer, withdraw
from tests.database import assert_reconciled
from tests.factories import FundedAccount, commit_funded_account, get_settlement_account

pytestmark = pytest.mark.concurrency


@pytest.fixture(autouse=True)
def no_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(posting_service, "MAX_ATTEMPTS", 1)


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


type Operation = Callable[[AsyncSession], Awaitable[PostingResult]]


async def _run_concurrently(
    sessionmaker: async_sessionmaker[AsyncSession], operations: list[Operation]
) -> list[PostingResult | BaseException]:
    """Each operation on its own session (own connection), all started together."""
    start = asyncio.Event()

    async def run(operation: Operation) -> PostingResult:
        async with sessionmaker() as session:
            await start.wait()
            return await operation(session)

    tasks = [asyncio.create_task(run(operation)) for operation in operations]
    await asyncio.sleep(0)  # let every task reach the start line
    start.set()
    return list(await asyncio.gather(*tasks, return_exceptions=True))


def _outcomes(results: list[PostingResult | BaseException]) -> Counter[str]:
    return Counter(type(result).__name__ for result in results)


async def _balances(
    sessionmaker: async_sessionmaker[AsyncSession], account_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    async with sessionmaker() as session:
        rows = await session.execute(
            select(Account.id, Account.balance_minor).where(Account.id.in_(account_ids))
        )
        return {account_id: balance for account_id, balance in rows}


def _transfer(source: FundedAccount, destination: FundedAccount, amount: int) -> Operation:
    async def operation(session: AsyncSession) -> PostingResult:
        return await transfer(
            session,
            owner_id=source.owner_id,
            source_account_id=source.account_id,
            destination_account_id=destination.account_id,
            amount_minor=amount,
        )

    return operation


async def test_concurrent_withdrawals_never_overdraw(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account = await commit_funded_account(sessionmaker, 100)

    async def withdraw_30(session: AsyncSession) -> PostingResult:
        return await withdraw(
            session, owner_id=account.owner_id, account_id=account.account_id, amount_minor=30
        )

    results = await _run_concurrently(sessionmaker, [withdraw_30] * 10)

    # Each withdrawal saw the balance left by the previous one (it waited for the lock).
    assert _outcomes(results) == {"PostingResult": 3, "InsufficientFundsError": 7}
    assert await _balances(sessionmaker, [account.account_id]) == {account.account_id: 10}
    async with sessionmaker() as session:
        await assert_reconciled(session)


async def test_opposite_transfers_do_not_deadlock(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    first = await commit_funded_account(sessionmaker, 10_000)
    second = await commit_funded_account(sessionmaker, 10_000)
    operations = [_transfer(first, second, 100), _transfer(second, first, 100)] * 20

    results = await _run_concurrently(sessionmaker, operations)

    # With retries disabled, any deadlock would appear as TemporarilyUnavailableError.
    assert _outcomes(results) == {"PostingResult": 40}
    assert await _balances(sessionmaker, [first.account_id, second.account_id]) == {
        first.account_id: 10_000,
        second.account_id: 10_000,
    }
    async with sessionmaker() as session:
        await assert_reconciled(session)


async def test_random_concurrent_transfers_conserve_money(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    accounts = [await commit_funded_account(sessionmaker, 1_000) for _ in range(5)]
    ids = [account.account_id for account in accounts]
    rng = random.Random(4)  # fixed seed: reproducible  # noqa: S311
    operations = []
    for _ in range(50):
        source, destination = rng.sample(accounts, 2)
        operations.append(_transfer(source, destination, rng.randint(1, 600)))

    results = await _run_concurrently(sessionmaker, operations)

    outcomes = _outcomes(results)
    assert set(outcomes) <= {"PostingResult", "InsufficientFundsError"}, outcomes
    balances = await _balances(sessionmaker, ids)
    assert sum(balances.values()) == 5 * 1_000  # money moved, none created or destroyed
    assert min(balances.values()) >= 0
    async with sessionmaker() as session:
        # Exactly the successful transfers were committed, each with its two entries.
        posted = await session.scalar(
            select(func.count(func.distinct(LedgerEntry.transaction_id)))
            .join(Transaction, Transaction.id == LedgerEntry.transaction_id)
            .where(LedgerEntry.account_id.in_(ids), Transaction.kind == TransactionKind.TRANSFER)
        )
        assert posted == outcomes["PostingResult"]
        await assert_reconciled(session)


async def test_concurrent_deposits_are_each_applied_exactly_once(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Every deposit also locks the currency's settlement account: they serialize on it.
    account = await commit_funded_account(sessionmaker, 0)
    async with sessionmaker() as session:
        settlement = await get_settlement_account(session, Currency.GBP)
    settlement_before = (await _balances(sessionmaker, [settlement.id]))[settlement.id]

    async def deposit_50(session: AsyncSession) -> PostingResult:
        return await deposit(
            session, owner_id=account.owner_id, account_id=account.account_id, amount_minor=50
        )

    results = await _run_concurrently(sessionmaker, [deposit_50] * 20)

    assert _outcomes(results) == {"PostingResult": 20}
    balances = await _balances(sessionmaker, [account.account_id, settlement.id])
    assert balances[account.account_id] == 1_000
    assert balances[settlement.id] == settlement_before - 1_000
    async with sessionmaker() as session:
        await assert_reconciled(session)


async def test_insufficient_funds_is_decided_on_the_balance_after_the_lock(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A withdrawal of the whole balance races a deposit: whichever order the locks are
    # granted in, the result is consistent with that order, never with a stale read.
    account = await commit_funded_account(sessionmaker, 500)

    async def withdraw_all_plus_one(session: AsyncSession) -> PostingResult:
        return await withdraw(
            session, owner_id=account.owner_id, account_id=account.account_id, amount_minor=501
        )

    async def deposit_one(session: AsyncSession) -> PostingResult:
        return await deposit(
            session, owner_id=account.owner_id, account_id=account.account_id, amount_minor=1
        )

    results = await _run_concurrently(sessionmaker, [withdraw_all_plus_one, deposit_one])

    final = (await _balances(sessionmaker, [account.account_id]))[account.account_id]
    if isinstance(results[0], InsufficientFundsError):
        assert final == 501  # the withdrawal locked first and saw 500
    else:
        assert final == 0  # the deposit committed first; the withdrawal saw 501
    assert isinstance(results[1], PostingResult)
