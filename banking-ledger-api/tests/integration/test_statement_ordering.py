"""ledger_entries.sequence_number orders one account's entries in commit order (ADR 0012).

Why: a posting draws the number for an account's entry while it holds that account's row lock
(FOR UPDATE), and keeps the lock until COMMIT. Two postings on one account therefore draw their
numbers inside non-overlapping lock intervals, and a CACHE 1 sequence only increases. Nothing is
claimed across different accounts.

Real connections and COMMITs; retries disabled so contention cannot be retried away.
"""

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger_api.data import posting as data_posting
from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.errors import sqlstate
from ledger_api.data.models import Account, LedgerEntry, Transaction
from ledger_api.domain.errors import InsufficientFundsError
from ledger_api.services import posting as posting_service
from ledger_api.services.posting import PostingResult, deposit, withdraw
from tests.database import assert_reconciled
from tests.factories import FundedAccount, commit_funded_account

pytestmark = pytest.mark.concurrency

LOCK_WAIT_POLL_SECONDS = 0.02
LOCK_WAIT_DEADLINE_SECONDS = 5


@pytest.fixture(autouse=True)
def no_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(posting_service, "MAX_ATTEMPTS", 1)


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


async def _entry(
    sessionmaker: async_sessionmaker[AsyncSession], transaction_id: uuid.UUID, account_id: uuid.UUID
) -> tuple[int, Any]:
    """(sequence_number, transaction created_at) of a transaction's entry on an account."""
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(LedgerEntry.sequence_number, Transaction.created_at)
                .join(Transaction, Transaction.id == LedgerEntry.transaction_id)
                .where(
                    LedgerEntry.transaction_id == transaction_id,
                    LedgerEntry.account_id == account_id,
                )
            )
        ).one()
    return row.sequence_number, row.created_at


async def _wait_until_a_backend_waits_for_a_lock(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        async with asyncio.timeout(LOCK_WAIT_DEADLINE_SECONDS):
            while not await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ):
                await connection.rollback()  # a fresh snapshot of pg_stat_activity
                await asyncio.sleep(LOCK_WAIT_POLL_SECONDS)


def _paused_in_task(
    name: str,
    real: Callable[..., Awaitable[Any]],
    reached: asyncio.Event,
    proceed: asyncio.Event,
    *,
    before: bool,
) -> Callable[..., Awaitable[Any]]:
    """Wrap a posting step so the task called `name` pauses before or after it."""

    async def step(*args: Any, **kwargs: Any) -> Any:
        task = asyncio.current_task()
        paused = task is not None and task.get_name() == name
        if paused and before:
            reached.set()
            await proceed.wait()
        result = await real(*args, **kwargs)
        if paused and not before:
            reached.set()
            await proceed.wait()
        return result

    return step


async def test_sequence_follows_lock_order_not_transaction_start(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await commit_funded_account(sessionmaker, 1_000)
    q_began, q_go = asyncio.Event(), asyncio.Event()
    p_wrote, p_go = asyncio.Event(), asyncio.Event()
    # Q pauses after BEGIN (its first statement fixed now()), before taking any lock.
    monkeypatch.setattr(
        posting_service,
        "lock_accounts",
        _paused_in_task("Q", data_posting.lock_accounts, q_began, q_go, before=True),
    )
    # P pauses after its entries are written, while it holds the account's lock.
    monkeypatch.setattr(
        posting_service,
        "apply_balance_deltas",
        _paused_in_task("P", data_posting.apply_balance_deltas, p_wrote, p_go, before=False),
    )

    async def run_deposit() -> PostingResult:
        async with sessionmaker() as session:
            return await deposit(
                session, owner_id=account.owner_id, account_id=account.account_id, amount_minor=5
            )

    async def run_withdrawal() -> PostingResult:
        async with sessionmaker() as session:
            return await withdraw(
                session, owner_id=account.owner_id, account_id=account.account_id, amount_minor=7
            )

    q = asyncio.create_task(run_deposit(), name="Q")
    await q_began.wait()  # Q's transaction started first
    p = asyncio.create_task(run_withdrawal(), name="P")
    await p_wrote.wait()  # P holds the lock and has drawn its sequence number
    q_go.set()
    await _wait_until_a_backend_waits_for_a_lock(engine)  # Q really is blocked on P's lock
    p_go.set()
    p_result, q_result = await asyncio.gather(p, q)

    p_sequence, p_started = await _entry(sessionmaker, p_result.transaction_id, account.account_id)
    q_sequence, q_started = await _entry(sessionmaker, q_result.transaction_id, account.account_id)
    assert q_started < p_started  # created_at would order them the wrong way round...
    assert p_sequence < q_sequence  # ...sequence_number follows the lock, hence commit, order


async def test_concurrent_postings_on_one_account_have_a_consistent_history(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Every withdrawal checked funds under the lock against the balance committed before it.
    # Replaying the account's entries in sequence order must therefore never go negative and
    # must end at the stored balance. A history ordered differently from the commits could
    # show a withdrawal before the deposit that funded it.
    account = await commit_funded_account(sessionmaker, 100)
    rng = random.Random(7)  # fixed seed: reproducible  # noqa: S311
    operations = [(rng.choice([deposit, withdraw]), rng.randint(1, 80)) for _ in range(30)]
    start = asyncio.Event()

    async def run(operation: Any, amount: int) -> PostingResult:
        async with sessionmaker() as session:
            await start.wait()
            result: PostingResult = await operation(
                session,
                owner_id=account.owner_id,
                account_id=account.account_id,
                amount_minor=amount,
            )
            return result

    tasks = [asyncio.create_task(run(operation, amount)) for operation, amount in operations]
    await asyncio.sleep(0)
    start.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert {type(result) for result in results} <= {PostingResult, InsufficientFundsError}
    async with sessionmaker() as session:
        amounts = (
            await session.scalars(
                select(LedgerEntry.amount_minor)
                .where(LedgerEntry.account_id == account.account_id)
                .order_by(LedgerEntry.sequence_number)
            )
        ).all()
        balance = await session.scalar(
            select(Account.balance_minor).where(Account.id == account.account_id)
        )
        await assert_reconciled(session)
    running = 0
    for amount in amounts:
        running += amount
        assert running >= 0
    assert running == balance
    assert len(amounts) == 1 + sum(isinstance(result, PostingResult) for result in results)


# --- schema ------------------------------------------------------------------------------------


async def test_sequence_cache_is_one(db_session: AsyncSession) -> None:
    # A larger cache hands each session its own block of numbers: no longer increasing in time.
    cache = await db_session.scalar(
        text(
            "SELECT seqcache FROM pg_sequence WHERE seqrelid = "
            "pg_get_serial_sequence('ledger_entries', 'sequence_number')::regclass"
        )
    )
    assert cache == 1


async def test_generated_always_refuses_an_explicit_number_without_override(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    # Only a plain INSERT is refused. OVERRIDING SYSTEM VALUE gets past GENERATED ALWAYS; the
    # ordering trigger of migration 0006 is what rejects backdated or unissued numbers then
    # (test_entry_sequence_order.py).
    funded = await _committed_entry(sessionmaker)

    with pytest.raises(DBAPIError) as excinfo:
        await db_session.execute(
            text(
                "INSERT INTO ledger_entries "
                "(transaction_id, account_id, ledger_id, amount_minor, sequence_number) "
                "SELECT transaction_id, account_id, ledger_id, 1, 1 FROM ledger_entries "
                "WHERE account_id = :account LIMIT 1"
            ),
            {"account": funded.account_id},
        )
    assert sqlstate(excinfo.value) == "428C9"  # generated_always


async def test_sequence_numbers_cannot_be_rewritten(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    # GENERATED ALWAYS refuses the UPDATE before the immutability trigger even runs.
    funded = await _committed_entry(sessionmaker)

    with pytest.raises(DBAPIError) as excinfo:
        await db_session.execute(
            text(
                "UPDATE ledger_entries SET sequence_number = sequence_number + 1000000 "
                "WHERE account_id = :account"
            ),
            {"account": funded.account_id},
        )
    assert sqlstate(excinfo.value) == "428C9"  # generated_always


async def _committed_entry(sessionmaker: async_sessionmaker[AsyncSession]) -> FundedAccount:
    return await commit_funded_account(sessionmaker, 10)
