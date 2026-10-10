"""Data-layer posting steps and the locking protocol (ADR 0010)."""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.errors import sqlstate
from ledger_api.data.models import Account, Ledger, LedgerEntry, Transaction
from ledger_api.data.posting import (
    apply_balance_deltas,
    find_settlement_account_id,
    insert_posting,
    lock_accounts,
)
from ledger_api.domain.account import AccountKind
from ledger_api.domain.currency import Currency
from ledger_api.domain.posting import deposit_posting
from tests.conftest import SessionFactory
from tests.database import assert_reconciled, check_deferred_constraints
from tests.factories import create_customer_account, get_ledger, get_settlement_account

LOCK_NOT_AVAILABLE = "55P03"


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
async def committed_accounts(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> list[uuid.UUID]:
    """Three committed GBP customer accounts (balance 0: the database stays reconciled)."""
    async with sessionmaker() as session, session.begin():
        accounts = [await create_customer_account(session) for _ in range(3)]
    return [account.id for account in accounts]


async def _can_lock_now(session: AsyncSession, table: str, row_id: uuid.UUID) -> bool:
    try:
        await session.execute(
            text(f"SELECT 1 FROM {table} WHERE id = :id FOR UPDATE NOWAIT"),  # noqa: S608
            {"id": row_id},
        )
    except DBAPIError as error:
        if sqlstate(error) != LOCK_NOT_AVAILABLE:
            raise
        return False
    return True


# --- locking -----------------------------------------------------------------------------------


async def test_lock_accounts_locks_exactly_the_requested_accounts(
    sessionmaker: async_sessionmaker[AsyncSession], committed_accounts: list[uuid.UUID]
) -> None:
    first, second, untouched = committed_accounts

    async with sessionmaker() as holder, holder.begin():
        await lock_accounts(holder, [second, first])

        for account_id, expected in ((first, False), (second, False), (untouched, True)):
            async with sessionmaker() as probe, probe.begin():
                assert await _can_lock_now(probe, "accounts", account_id) is expected


async def test_lock_accounts_never_locks_the_ledger_row(
    sessionmaker: async_sessionmaker[AsyncSession], committed_accounts: list[uuid.UUID]
) -> None:
    # A FOR UPDATE that joined ledgers would lock the ledger row and serialize every posting
    # in the currency; FOR UPDATE OF accounts must not.
    async with sessionmaker() as holder, holder.begin():
        await lock_accounts(holder, committed_accounts)
        ledger_id = await holder.scalar(select(Ledger.id).where(Ledger.currency == Currency.GBP))
        assert ledger_id is not None

        async with sessionmaker() as probe, probe.begin():
            assert await _can_lock_now(probe, "ledgers", ledger_id)


async def test_lock_accounts_returns_rows_in_lock_order(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    accounts = [await create_customer_account(db_session) for _ in range(4)]
    ids = [account.id for account in accounts]

    async with new_session() as session, session.begin():
        locked = await lock_accounts(session, list(reversed(ids)))

    assert list(locked) == sorted(ids)
    assert all(
        row.kind is AccountKind.CUSTOMER and row.balance_minor == 0 for row in locked.values()
    )


async def test_missing_accounts_are_absent_from_the_lock_result(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    account = await create_customer_account(db_session)

    async with new_session() as session, session.begin():
        locked = await lock_accounts(session, [account.id, uuid.uuid7()])

    assert list(locked) == [account.id]


async def test_locked_state_is_fresh_even_when_the_identity_map_is_stale(
    sessionmaker: async_sessionmaker[AsyncSession], committed_accounts: list[uuid.UUID]
) -> None:
    account_id = committed_accounts[0]
    async with sessionmaker() as reader:
        # An earlier transaction on this session leaves the Account in the identity map
        # (expire_on_commit=False keeps its attributes): balance 0.
        async with reader.begin():
            stale = await reader.get(Account, account_id)
        assert stale is not None
        assert stale.balance_minor == 0

        # Another connection deposits 700 and commits (balances stay reconciled).
        async with sessionmaker() as writer, writer.begin():
            settlement = await find_settlement_account_id(writer, account_id=account_id)
            assert settlement is not None
            posting = deposit_posting(
                account_id=account_id, settlement_account_id=settlement, amount_minor=700
            )
            locked = await lock_accounts(writer, posting.account_ids)
            await insert_posting(writer, posting, ledger_id=locked[account_id].ledger_id)
            await apply_balance_deltas(
                writer, {line.account_id: line.amount_minor for line in posting.lines}
            )

        async with reader.begin():
            locked = await lock_accounts(reader, [account_id])

        # The ORM object would still say 0; the locking statement's row is current.
        assert stale.balance_minor == 0
        assert locked[account_id].balance_minor == 700


# --- settlement lookup -------------------------------------------------------------------------


@pytest.mark.parametrize("currency", list(Currency))
async def test_settlement_account_is_the_one_of_the_accounts_ledger(
    db_session: AsyncSession, currency: Currency
) -> None:
    account = await create_customer_account(db_session, currency)
    settlement = await get_settlement_account(db_session, currency)

    assert await find_settlement_account_id(db_session, account_id=account.id) == settlement.id


async def test_settlement_lookup_for_an_unknown_account_is_none(db_session: AsyncSession) -> None:
    assert await find_settlement_account_id(db_session, account_id=uuid.uuid7()) is None


async def test_settlement_lookup_for_a_system_account_is_none(db_session: AsyncSession) -> None:
    # Otherwise a deposit "into" the settlement account would pair it with itself.
    settlement = await get_settlement_account(db_session, Currency.GBP)

    assert await find_settlement_account_id(db_session, account_id=settlement.id) is None


# --- writing a posting -------------------------------------------------------------------------


async def test_insert_posting_and_balance_deltas_produce_a_reconciled_ledger(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    account = await create_customer_account(db_session)
    settlement = await get_settlement_account(db_session, Currency.GBP)
    ledger = await get_ledger(db_session, Currency.GBP)
    posting = deposit_posting(
        account_id=account.id, settlement_account_id=settlement.id, amount_minor=1_250
    )

    async with new_session() as session, session.begin():
        await lock_accounts(session, posting.account_ids)
        transaction = await insert_posting(session, posting, ledger_id=ledger.id)
        balances = await apply_balance_deltas(
            session, {line.account_id: line.amount_minor for line in posting.lines}
        )
        await check_deferred_constraints(session)

    assert balances[account.id] == 1_250
    stored = await db_session.get(Transaction, transaction.id)
    assert stored is not None
    assert (stored.kind, stored.entry_count) == (posting.kind, 2)
    entries = (
        await db_session.scalars(
            select(LedgerEntry.amount_minor).where(LedgerEntry.transaction_id == transaction.id)
        )
    ).all()
    assert sorted(entries) == [-1_250, 1_250]
    await assert_reconciled(db_session)
