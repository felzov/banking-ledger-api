"""Transient database failures: what is retried (the whole transaction), what is not, and
how contention is reported (ADR 0005, ADR 0010)."""

import asyncio
import time
import uuid
from collections.abc import Collection
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger_api.data import posting as data_posting
from ledger_api.data.engine import LOCK_TIMEOUT_MS, create_sessionmaker
from ledger_api.data.errors import sqlstate
from ledger_api.data.models import Account, Transaction
from ledger_api.data.posting import LockedAccount
from ledger_api.domain.errors import InsufficientFundsError, TemporarilyUnavailableError
from ledger_api.services import posting as posting_service
from ledger_api.services.posting import PostingResult, transfer, withdraw
from tests.database import assert_reconciled
from tests.factories import FundedAccount, commit_funded_account


class _DriverError(Exception):
    """Stands in for the driver exception; carries the SQLSTATE like asyncpg's errors do."""

    def __init__(self, code: str) -> None:
        super().__init__(f"SQLSTATE {code}")
        self.sqlstate = code


def _database_error(code: str) -> DBAPIError:
    return DBAPIError("SELECT ... FOR UPDATE", None, _DriverError(code))


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
async def account(sessionmaker: async_sessionmaker[AsyncSession]) -> FundedAccount:
    return await commit_funded_account(sessionmaker, 5_000)


class _FailingLocks:
    """Replaces lock_accounts: fails the first `failures` calls with SQLSTATE `code`."""

    def __init__(self, code: str, failures: int) -> None:
        self.code, self.failures, self.calls = code, failures, 0

    async def __call__(
        self, session: AsyncSession, account_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, LockedAccount]:
        self.calls += 1
        if self.calls <= self.failures:
            raise _database_error(self.code)
        return await data_posting.lock_accounts(session, account_ids)


async def _withdraw(
    sessionmaker: async_sessionmaker[AsyncSession], account: FundedAccount, amount: int = 1_000
) -> PostingResult:
    async with sessionmaker() as session:
        return await withdraw(
            session, owner_id=account.owner_id, account_id=account.account_id, amount_minor=amount
        )


async def _balance(sessionmaker: async_sessionmaker[AsyncSession], account_id: uuid.UUID) -> int:
    async with sessionmaker() as session:
        balance = await session.scalar(
            select(Account.balance_minor).where(Account.id == account_id)
        )
    assert balance is not None
    return balance


# --- retry policy ------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["40P01", "40001"])
async def test_transient_failures_retry_the_whole_transaction(
    sessionmaker: async_sessionmaker[AsyncSession],
    account: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    locks = _FailingLocks(code, failures=2)
    monkeypatch.setattr(posting_service, "lock_accounts", locks)

    result = await _withdraw(sessionmaker, account)

    assert locks.calls == 3  # two failed attempts, then success on the last allowed one
    assert result.balances == {account.account_id: 4_000}
    async with sessionmaker() as session:
        await assert_reconciled(session)


async def test_exhausted_retries_report_temporary_unavailability(
    sessionmaker: async_sessionmaker[AsyncSession],
    account: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locks = _FailingLocks("40P01", failures=10)
    monkeypatch.setattr(posting_service, "lock_accounts", locks)

    with pytest.raises(TemporarilyUnavailableError):
        await _withdraw(sessionmaker, account)

    assert locks.calls == posting_service.MAX_ATTEMPTS
    assert await _balance(sessionmaker, account.account_id) == 5_000


async def test_retries_can_be_disabled(
    sessionmaker: async_sessionmaker[AsyncSession],
    account: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locks = _FailingLocks("40P01", failures=1)
    monkeypatch.setattr(posting_service, "lock_accounts", locks)
    monkeypatch.setattr(posting_service, "MAX_ATTEMPTS", 1)

    with pytest.raises(TemporarilyUnavailableError):
        await _withdraw(sessionmaker, account)

    assert locks.calls == 1


@pytest.mark.parametrize("code", ["55P03", "57014"])
async def test_timeouts_are_reported_without_retrying(
    sessionmaker: async_sessionmaker[AsyncSession],
    account: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    locks = _FailingLocks(code, failures=1)
    monkeypatch.setattr(posting_service, "lock_accounts", locks)

    with pytest.raises(TemporarilyUnavailableError):
        await _withdraw(sessionmaker, account)

    assert locks.calls == 1


async def test_other_database_errors_are_neither_retried_nor_translated(
    sessionmaker: async_sessionmaker[AsyncSession],
    account: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locks = _FailingLocks("23514", failures=1)  # check_violation: a bug, not contention
    monkeypatch.setattr(posting_service, "lock_accounts", locks)

    with pytest.raises(DBAPIError) as excinfo:
        await _withdraw(sessionmaker, account)

    assert not isinstance(excinfo.value, TemporarilyUnavailableError)
    assert locks.calls == 1


async def test_business_rejections_are_not_retried(
    sessionmaker: async_sessionmaker[AsyncSession],
    account: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locks = _FailingLocks("40P01", failures=0)
    monkeypatch.setattr(posting_service, "lock_accounts", locks)

    with pytest.raises(InsufficientFundsError):
        await _withdraw(sessionmaker, account, amount=5_001)

    assert locks.calls == 1


# --- real PostgreSQL behaviour -----------------------------------------------------------------


def _lock_one_at_a_time_in_caller_order(barrier: asyncio.Barrier) -> Any:
    """A deliberately WRONG locking path: one row at a time, in the order the task name gives,
    pausing between the two locks the first time. Two such paths with opposite orders
    deadlock for real. The protocol's single sorted statement exists to prevent exactly this.
    """
    waited: set[str] = set()

    async def lock(
        session: AsyncSession, account_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, LockedAccount]:
        task = asyncio.current_task()
        name = task.get_name() if task else ""
        order = sorted(account_ids, reverse=name == "reverse")
        for position, account_id in enumerate(order):
            await session.execute(
                select(Account.id).where(Account.id == account_id).with_for_update(of=Account)
            )
            if position == 0 and name not in waited:
                waited.add(name)
                await barrier.wait()  # both hold their first lock: the second ones collide
        return await data_posting.lock_accounts(session, account_ids)

    return lock


async def _opposite_transfers(
    sessionmaker: async_sessionmaker[AsyncSession], first: FundedAccount, second: FundedAccount
) -> list[PostingResult | BaseException]:
    async def send(source: FundedAccount, destination: FundedAccount) -> PostingResult:
        async with sessionmaker() as session:
            return await transfer(
                session,
                owner_id=source.owner_id,
                source_account_id=source.account_id,
                destination_account_id=destination.account_id,
                amount_minor=100,
            )

    forward = asyncio.create_task(send(first, second), name="forward")
    reverse = asyncio.create_task(send(second, first), name="reverse")
    return list(await asyncio.gather(forward, reverse, return_exceptions=True))


async def test_a_real_deadlock_is_detected_and_retried(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await commit_funded_account(sessionmaker, 1_000)
    second = await commit_funded_account(sessionmaker, 1_000)
    monkeypatch.setattr(
        posting_service, "lock_accounts", _lock_one_at_a_time_in_caller_order(asyncio.Barrier(2))
    )

    results = await _opposite_transfers(sessionmaker, first, second)

    # PostgreSQL aborted one transaction (40P01); its retry then succeeded.
    assert all(isinstance(result, PostingResult) for result in results), results
    assert await _balance(sessionmaker, first.account_id) == 1_000
    assert await _balance(sessionmaker, second.account_id) == 1_000
    async with sessionmaker() as session:
        await assert_reconciled(session)


async def test_without_retries_the_real_deadlock_surfaces(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await commit_funded_account(sessionmaker, 1_000)
    second = await commit_funded_account(sessionmaker, 1_000)
    monkeypatch.setattr(
        posting_service, "lock_accounts", _lock_one_at_a_time_in_caller_order(asyncio.Barrier(2))
    )
    monkeypatch.setattr(posting_service, "MAX_ATTEMPTS", 1)

    results = await _opposite_transfers(sessionmaker, first, second)

    assert sorted(type(result).__name__ for result in results) == [
        "PostingResult",
        "TemporarilyUnavailableError",
    ]
    # ...and what PostgreSQL reported was a deadlock, not a lock timeout.
    unavailable = next(r for r in results if isinstance(r, TemporarilyUnavailableError))
    assert isinstance(unavailable.__cause__, DBAPIError)
    assert sqlstate(unavailable.__cause__) == "40P01"
    async with sessionmaker() as session:
        await assert_reconciled(session)


async def test_a_held_lock_times_out_as_temporarily_unavailable(
    sessionmaker: async_sessionmaker[AsyncSession], account: FundedAccount
) -> None:
    async with sessionmaker() as session:
        transactions_before = await session.scalar(select(func.count()).select_from(Transaction))

    async with sessionmaker() as holder, holder.begin():
        await data_posting.lock_accounts(holder, [account.account_id])
        started = time.monotonic()

        with pytest.raises(TemporarilyUnavailableError):
            await _withdraw(sessionmaker, account)

        waited = time.monotonic() - started
    # It waited for lock_timeout, and did not then retry (which would take a multiple of it).
    assert LOCK_TIMEOUT_MS / 1000 * 0.9 <= waited < LOCK_TIMEOUT_MS / 1000 * 2
    async with sessionmaker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Transaction))
            == transactions_before
        )
    assert await _balance(sessionmaker, account.account_id) == 5_000
