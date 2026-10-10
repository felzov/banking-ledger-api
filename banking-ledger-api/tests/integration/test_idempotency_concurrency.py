"""Concurrent requests with idempotency keys, on real connections with real COMMITs (ADR 0006).

Retries are DISABLED (MAX_ATTEMPTS = 1), as in test_posting_concurrency: a deadlock would
surface as a failure. Same-key requests coordinate on the key's unique index alone: the second
claim waits for the first transaction, then replays its committed result, or runs if it rolled
back. No Redis, no advisory lock, no SELECT-then-INSERT race.
"""

import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Collection

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger_api.data.engine import LOCK_TIMEOUT_MS, create_sessionmaker
from ledger_api.data.models import Account, IdempotencyKey, LedgerEntry
from ledger_api.data.posting import LockedAccount, lock_accounts
from ledger_api.domain.audit import AuditOutcome
from ledger_api.domain.errors import (
    IdempotencyKeyMismatchError,
    InsufficientFundsError,
    TemporarilyUnavailableError,
)
from ledger_api.services import posting as posting_service
from ledger_api.services.posting import PostingResult, transfer, withdraw
from tests.database import assert_reconciled, audit_events_about
from tests.factories import FundedAccount, commit_funded_account

pytestmark = pytest.mark.concurrency

type Maker = async_sessionmaker[AsyncSession]
type Operation = Callable[[AsyncSession], Awaitable[PostingResult]]


class InjectedFailureError(Exception):
    pass


@pytest.fixture(autouse=True)
def no_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(posting_service, "MAX_ATTEMPTS", 1)


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> Maker:
    return create_sessionmaker(engine)


def _key() -> str:
    return f"race-{uuid.uuid7().hex}"


def _withdrawal(account: FundedAccount, amount: int, key: str) -> Operation:
    async def operation(session: AsyncSession) -> PostingResult:
        return await withdraw(
            session,
            owner_id=account.owner_id,
            account_id=account.account_id,
            amount_minor=amount,
            idempotency_key=key,
        )

    return operation


async def _run_concurrently(
    sessionmaker: Maker, operations: list[Operation]
) -> list[PostingResult | BaseException]:
    """Each operation on its own session (own connection), all started together."""
    start = asyncio.Event()

    async def run(operation: Operation) -> PostingResult:
        async with sessionmaker() as session:
            await start.wait()
            return await operation(session)

    tasks = [asyncio.create_task(run(operation)) for operation in operations]
    await asyncio.sleep(0)
    start.set()
    return list(await asyncio.gather(*tasks, return_exceptions=True))


def _outcomes(results: list[PostingResult | BaseException]) -> Counter[str]:
    return Counter(
        ("replayed" if result.replayed else "posted")
        if isinstance(result, PostingResult)
        else type(result).__name__
        for result in results
    )


async def _balance(sessionmaker: Maker, account: FundedAccount) -> int:
    async with sessionmaker() as session:
        balance = await session.scalar(
            select(Account.balance_minor).where(Account.id == account.account_id)
        )
    assert balance is not None
    return balance


async def _entry_count(sessionmaker: Maker, account: FundedAccount) -> int:
    async with sessionmaker() as session:
        count = await session.scalar(
            select(func.count()).where(LedgerEntry.account_id == account.account_id)
        )
    return count or 0


async def _key_row(sessionmaker: Maker, account: FundedAccount, key: str) -> IdempotencyKey | None:
    async with sessionmaker() as session:
        return await session.get(IdempotencyKey, (account.owner_id, key))


async def _assert_reconciled(sessionmaker: Maker) -> None:
    async with sessionmaker() as session:
        await assert_reconciled(session)


async def _wait_for_a_blocked_claim(engine: AsyncEngine) -> None:
    """Until some backend is waiting for another transaction to end (the key's index entry)."""
    deadline = time.monotonic() + 5
    async with engine.connect() as observer:
        while time.monotonic() < deadline:
            waiting = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'transactionid' AND NOT granted"
                )
            )
            await observer.rollback()
            if waiting:
                return
            await asyncio.sleep(0.02)
    pytest.fail("the retry never blocked on the original's claim")


# --- same key, same request ---------------------------------------------------------------------


async def test_ten_identical_requests_post_once_and_nine_replay(sessionmaker: Maker) -> None:
    account = await commit_funded_account(sessionmaker, 5_000)
    entries = await _entry_count(sessionmaker, account)
    key = _key()

    results = await _run_concurrently(sessionmaker, [_withdrawal(account, 700, key)] * 10)

    assert _outcomes(results) == {"posted": 1, "replayed": 9}
    posted = [result for result in results if isinstance(result, PostingResult)]
    assert {result.transaction_id for result in posted} == {posted[0].transaction_id}
    # Every caller, replay or not, sees the balance right after THE posting.
    assert {result.balances[account.account_id] for result in posted} == {4_300}
    assert await _balance(sessionmaker, account) == 4_300
    assert await _entry_count(sessionmaker, account) == entries + 1
    row = await _key_row(sessionmaker, account, key)
    assert row is not None
    assert row.transaction_id == posted[0].transaction_id
    async with sessionmaker() as session:
        _funding, *events = await audit_events_about(session, account.account_id)
    assert [(event.outcome, event.transaction_id) for event in events] == [
        (AuditOutcome.SUCCEEDED, posted[0].transaction_id)  # replays are not audited
    ]
    await _assert_reconciled(sessionmaker)


# --- same key, different requests ---------------------------------------------------------------


async def test_same_key_with_mixed_requests_posts_one_of_them_once(sessionmaker: Maker) -> None:
    account = await commit_funded_account(sessionmaker, 5_000)
    key = _key()
    operations = [_withdrawal(account, 100, key), _withdrawal(account, 200, key)] * 5

    results = await _run_concurrently(sessionmaker, operations)

    # Whichever claimed first posted; its twins replay it, the other group is refused.
    outcomes = _outcomes(results)
    assert outcomes == {"posted": 1, "replayed": 4, "IdempotencyKeyMismatchError": 5}
    [winner] = [r for r in results if isinstance(r, PostingResult) and not r.replayed]
    amount = 5_000 - winner.balances[account.account_id]
    assert amount in (100, 200)
    losers = [
        result
        for result, operation_amount in zip(results, [100, 200] * 5, strict=True)
        if operation_amount != amount
    ]
    assert all(isinstance(result, IdempotencyKeyMismatchError) for result in losers)
    assert await _balance(sessionmaker, account) == 5_000 - amount
    async with sessionmaker() as session:
        _funding, *events = await audit_events_about(session, account.account_id)
    assert Counter((event.outcome, event.reason) for event in events) == {
        (AuditOutcome.SUCCEEDED, None): 1,
        (AuditOutcome.REJECTED, "idempotency_key_mismatch"): 5,
    }
    await _assert_reconciled(sessionmaker)


# --- the first holder of the key fails, or takes too long ---------------------------------------


async def test_a_waiting_request_runs_when_the_first_holder_rolls_back(
    sessionmaker: Maker,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await commit_funded_account(sessionmaker, 5_000)
    key = _key()
    claimed, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def first_holder_fails(
        session: AsyncSession, account_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, LockedAccount]:
        nonlocal calls
        calls += 1
        if calls == 1:  # the first request: it has claimed the key, and now fails
            claimed.set()
            await release.wait()
            raise InjectedFailureError
        return await lock_accounts(session, account_ids)

    monkeypatch.setattr(posting_service, "lock_accounts", first_holder_fails)

    async def run(operation: Operation) -> PostingResult:
        async with sessionmaker() as session:
            return await operation(session)

    first = asyncio.create_task(run(_withdrawal(account, 700, key)))
    await claimed.wait()
    second = asyncio.create_task(run(_withdrawal(account, 700, key)))
    await _wait_for_a_blocked_claim(engine)  # the second waits on the first's claim...
    release.set()

    with pytest.raises(InjectedFailureError):
        await first
    retried = await second  # ...which rolled back: its own claim went ahead, and it posted

    assert not retried.replayed
    assert await _balance(sessionmaker, account) == 4_300
    row = await _key_row(sessionmaker, account, key)
    assert row is not None
    assert row.transaction_id == retried.transaction_id
    await _assert_reconciled(sessionmaker)


async def test_a_holder_beyond_lock_timeout_makes_the_waiter_unavailable_then_replayable(
    sessionmaker: Maker,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await commit_funded_account(sessionmaker, 5_000)
    key = _key()
    claimed = asyncio.Event()
    calls = 0

    async def slow_first_holder(
        session: AsyncSession, account_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, LockedAccount]:
        nonlocal calls
        calls += 1
        if calls == 1:  # holds its claim (uncommitted) longer than lock_timeout
            claimed.set()
            await asyncio.sleep(LOCK_TIMEOUT_MS / 1000 + 0.5)
        return await lock_accounts(session, account_ids)

    monkeypatch.setattr(posting_service, "lock_accounts", slow_first_holder)

    async def run(operation: Operation) -> PostingResult:
        async with sessionmaker() as session:
            return await operation(session)

    first = asyncio.create_task(run(_withdrawal(account, 700, key)))
    await claimed.wait()

    # The waiter gives up after lock_timeout: 503, nothing written, no key of its own.
    with pytest.raises(TemporarilyUnavailableError):
        await run(_withdrawal(account, 700, key))
    original = await first
    replayed = await run(_withdrawal(account, 700, key))  # the client's retry

    assert not original.replayed
    assert replayed.replayed
    assert replayed.transaction_id == original.transaction_id
    assert await _balance(sessionmaker, account) == 4_300
    async with sessionmaker() as session:
        _funding, *events = await audit_events_about(session, account.account_id)
    assert Counter((event.outcome, event.reason) for event in events) == {
        (AuditOutcome.FAILED, "temporarily_unavailable"): 1,
        (AuditOutcome.SUCCEEDED, None): 1,
    }
    await _assert_reconciled(sessionmaker)


# --- different keys: the posting protocol is unchanged ------------------------------------------


async def test_keyed_concurrent_withdrawals_never_overdraw(sessionmaker: Maker) -> None:
    account = await commit_funded_account(sessionmaker, 100)
    keys = [_key() for _ in range(10)]

    results = await _run_concurrently(sessionmaker, [_withdrawal(account, 30, key) for key in keys])

    assert _outcomes(results) == {"posted": 3, "InsufficientFundsError": 7}
    assert await _balance(sessionmaker, account) == 10
    rejected_keys = [
        key
        for key, result in zip(keys, results, strict=True)
        if isinstance(result, InsufficientFundsError)
    ]
    for key in rejected_keys:  # a rejection never consumes its key
        assert await _key_row(sessionmaker, account, key) is None
    await _assert_reconciled(sessionmaker)


async def test_keyed_opposite_transfers_do_not_deadlock(sessionmaker: Maker) -> None:
    first = await commit_funded_account(sessionmaker, 10_000)
    second = await commit_funded_account(sessionmaker, 10_000)

    def keyed_transfer(source: FundedAccount, destination: FundedAccount) -> Operation:
        key = _key()

        async def operation(session: AsyncSession) -> PostingResult:
            return await transfer(
                session,
                owner_id=source.owner_id,
                source_account_id=source.account_id,
                destination_account_id=destination.account_id,
                amount_minor=100,
                idempotency_key=key,
            )

        return operation

    operations: list[Operation] = []
    for _ in range(20):
        operations += [keyed_transfer(first, second), keyed_transfer(second, first)]

    results = await _run_concurrently(sessionmaker, operations)

    # Claim first, then the sorted account lock: still no deadlock (retries are disabled).
    assert _outcomes(results) == {"posted": 40}
    assert await _balance(sessionmaker, first) == await _balance(sessionmaker, second) == 10_000
    await _assert_reconciled(sessionmaker)
