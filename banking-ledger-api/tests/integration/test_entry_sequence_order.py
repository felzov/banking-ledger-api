"""An entry can only be appended to its account's history (migration 0006, ADR 0012).

GENERATED ALWAYS alone does not stop INSERT ... OVERRIDING SYSTEM VALUE. The
ledger_entries_sequence_order trigger does: a new entry must be numbered after the account's
latest entry and no later than the last number the sequence issued.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from ledger_api.data import posting as data_posting
from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.errors import violated_constraint
from ledger_api.data.models import Account, LedgerEntry, Transaction
from ledger_api.services import posting as posting_service
from ledger_api.services.posting import PostingResult, deposit
from tests.database import assert_reconciled, check_deferred_constraints, raises_violation
from tests.factories import (
    FundedAccount,
    add_entries,
    commit_funded_account,
    create_customer_account,
    create_transaction,
)

MONOTONIC = "ck_ledger_entries_sequence_monotonic"
ISSUED = "ck_ledger_entries_sequence_issued"

INSERT_HEADER = text(
    "INSERT INTO transactions (ledger_id, kind, entry_count) "
    "VALUES (:ledger, 'transfer', 2) RETURNING id"
)
INSERT_NUMBERED_ENTRIES = text(
    "INSERT INTO ledger_entries "
    "(transaction_id, account_id, ledger_id, amount_minor, sequence_number) "
    "OVERRIDING SYSTEM VALUE VALUES "
    "(:transaction, :payer, :ledger, -100, :payer_number), "
    "(:transaction, :payee, :ledger, 100, :payee_number)"
)


async def _latest_number(session: AsyncSession, account: Account) -> int:
    number = await session.scalar(
        select(func.max(LedgerEntry.sequence_number)).where(LedgerEntry.account_id == account.id)
    )
    assert number is not None
    return number


async def _last_issued(session: AsyncSession | AsyncConnection) -> int:
    issued = await session.scalar(
        text(
            "SELECT pg_sequence_last_value("
            "pg_get_serial_sequence('ledger_entries', 'sequence_number')::regclass)"
        )
    )
    assert isinstance(issued, int)
    return issued


async def _posted_pair(session: AsyncSession) -> tuple[Account, Account]:
    """Two accounts that already have a posted, checked history."""
    payer = await create_customer_account(session, balance_minor=1_000)
    payee = await create_customer_account(session)
    transaction = await create_transaction(session)
    add_entries(session, transaction, (payer, -500), (payee, 500))
    await check_deferred_constraints(session)
    return payer, payee


async def _insert_numbered_posting(
    session: AsyncSession, payer: Account, payee: Account, payer_number: int, payee_number: int
) -> None:
    """A balanced posting, sealed with its true entry count, numbered by the caller."""
    # check_deferred_constraints() made the checks immediate for the rest of the transaction;
    # defer them again, as in a real posting, so the header may precede its entries.
    await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    transaction_id = await session.scalar(INSERT_HEADER, {"ledger": payer.ledger_id})
    await session.execute(
        INSERT_NUMBERED_ENTRIES,
        {
            "transaction": transaction_id,
            "payer": payer.id,
            "payee": payee.id,
            "ledger": payer.ledger_id,
            "payer_number": payer_number,
            "payee_number": payee_number,
        },
    )
    await check_deferred_constraints(session)


# --- single session (rolled back) --------------------------------------------------------------


async def test_backdated_balanced_sealed_posting_is_rejected(db_session: AsyncSession) -> None:
    # Balanced, sealed, in one ledger: every other rule passes. Only the order does not.
    payer, payee = await _posted_pair(db_session)

    async with raises_violation(MONOTONIC):
        await _insert_numbered_posting(db_session, payer, payee, 1, 2)


async def test_number_equal_to_the_accounts_latest_is_rejected(db_session: AsyncSession) -> None:
    payer, payee = await _posted_pair(db_session)
    latest = await _latest_number(db_session, payer)
    issued = await _last_issued(db_session)

    async with raises_violation(MONOTONIC):
        await _insert_numbered_posting(db_session, payer, payee, latest, issued)


async def test_one_backdated_line_rejects_the_whole_posting(db_session: AsyncSession) -> None:
    # The payee line is fine (after its account's latest, already issued); the payer's is not.
    payer, _ = await _posted_pair(db_session)
    fresh_payee = await create_customer_account(db_session)
    issued = await _last_issued(db_session)

    async with raises_violation(MONOTONIC):
        await _insert_numbered_posting(db_session, payer, fresh_payee, 1, issued)


async def test_number_never_issued_by_the_sequence_is_rejected(db_session: AsyncSession) -> None:
    # Jumping ahead would make every later, legitimate entry of the account look backdated.
    payer, payee = await _posted_pair(db_session)
    issued = await _last_issued(db_session)

    async with raises_violation(ISSUED):
        await _insert_numbered_posting(db_session, payer, payee, issued + 1_000, issued + 1_001)


async def test_generated_numbers_keep_working_across_accounts(db_session: AsyncSession) -> None:
    # Normal inserts (sequence-generated numbers) on several accounts, some sharing a posting.
    payer, payee = await _posted_pair(db_session)
    third = await create_customer_account(db_session)
    await db_session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    for first, second in ((payer, third), (payee, payer), (third, payee)):
        transaction = await create_transaction(db_session)
        add_entries(db_session, transaction, (first, -10), (second, 10))
    await check_deferred_constraints(db_session)

    for account in (payer, payee, third):
        numbers = (
            await db_session.scalars(
                select(LedgerEntry.sequence_number)
                .where(LedgerEntry.account_id == account.id)
                .order_by(LedgerEntry.id)
            )
        ).all()
        assert list(numbers) == sorted(numbers)


async def test_residual_capability_an_issued_later_number_is_accepted(
    db_session: AsyncSession,
) -> None:
    # Documents the limit of the rule: OVERRIDING can still pick a number that is after the
    # account's latest and was already issued (to another account's entry). The history stays
    # append-only and every later generated number is still higher, so ordering holds.
    payer, _ = await _posted_pair(db_session)
    issued = await _last_issued(db_session)
    assert issued > await _latest_number(db_session, payer)
    fresh_payer = await create_customer_account(db_session, balance_minor=100)
    fresh_payee = await create_customer_account(db_session)

    await _insert_numbered_posting(db_session, fresh_payer, fresh_payee, issued - 1, issued)


# --- concurrent: the check cannot be raced ------------------------------------------------------


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
def no_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(posting_service, "MAX_ATTEMPTS", 1)


@pytest.fixture
async def raw_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        try:
            yield connection
        finally:
            await connection.rollback()  # never commit what the raw writer attempted


def _pause_after(
    real: Callable[..., Awaitable[Any]], task_name: str, reached: asyncio.Event, go: asyncio.Event
) -> Callable[..., Awaitable[Any]]:
    async def step(*args: Any, **kwargs: Any) -> Any:
        result = await real(*args, **kwargs)
        task = asyncio.current_task()
        if task is not None and task.get_name() == task_name:
            reached.set()
            await go.wait()
        return result

    return step


async def _wait_for_a_lock_wait(engine: AsyncEngine) -> None:
    async with engine.connect() as probe, asyncio.timeout(5):
        while not await probe.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        ):
            await probe.rollback()  # a fresh pg_stat_activity snapshot
            await asyncio.sleep(0.02)


@pytest.mark.concurrency
@pytest.mark.usefixtures("no_retries")
async def test_backdating_cannot_race_an_uncommitted_posting(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    raw_connection: AsyncConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A posting on account A has drawn its number N and holds A's lock, uncommitted. A raw
    # writer that does not take the lock picks M+1, between A's committed latest M and N.
    # Without the trigger's lock it would see only M, pass, and commit an entry before N.
    account: FundedAccount = await commit_funded_account(sessionmaker, 1_000)
    await commit_funded_account(sessionmaker, 1_000)  # draws numbers: leaves room after M
    async with sessionmaker() as session:
        committed_latest = await session.scalar(
            select(func.max(LedgerEntry.sequence_number)).where(
                LedgerEntry.account_id == account.account_id
            )
        )
        ledger_id = await session.scalar(
            select(Account.ledger_id).where(Account.id == account.account_id)
        )
        other = await create_customer_account(session)  # the raw posting's counterparty
        await session.commit()
    assert committed_latest is not None

    wrote, go = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        posting_service,
        "apply_balance_deltas",
        _pause_after(data_posting.apply_balance_deltas, "posting", wrote, go),
    )

    async def run_posting() -> PostingResult:
        async with sessionmaker() as session:
            return await deposit(
                session, owner_id=account.owner_id, account_id=account.account_id, amount_minor=7
            )

    async def backdate() -> None:
        transaction_id = await raw_connection.scalar(INSERT_HEADER, {"ledger": ledger_id})
        await raw_connection.execute(
            INSERT_NUMBERED_ENTRIES,
            {
                "transaction": transaction_id,
                "payer": other.id,
                "payee": account.account_id,
                "ledger": ledger_id,
                "payer_number": committed_latest + 1,
                "payee_number": committed_latest + 1,
            },
        )

    posting = asyncio.create_task(run_posting(), name="posting")
    await wrote.wait()  # the posting holds A's lock and has numbered its entry
    raw = asyncio.create_task(backdate())
    # The raw writer waits for A's row lock. (Its foreign-key check would wait too, but only
    # after the order check: what proves the trigger's lock is the rejection below.)
    await _wait_for_a_lock_wait(engine)
    assert not raw.done()
    go.set()

    result = await posting
    with pytest.raises(IntegrityError) as excinfo:
        await raw
    assert violated_constraint(excinfo.value) == MONOTONIC
    async with sessionmaker() as session:
        posted_number = await session.scalar(
            select(LedgerEntry.sequence_number).where(
                LedgerEntry.transaction_id == result.transaction_id,
                LedgerEntry.account_id == account.account_id,
            )
        )
        assert posted_number is not None
        assert committed_latest + 1 < posted_number  # the raw number really was "in between"
        assert await session.get(Transaction, result.transaction_id) is not None
        await assert_reconciled(session)
