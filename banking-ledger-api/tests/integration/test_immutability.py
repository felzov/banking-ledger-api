"""Posted ledger records are append-only (ADR 0004)."""

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import Account, LedgerEntry, Transaction
from ledger_api.domain.transaction import TransactionKind
from tests.database import check_deferred_constraints, raises_violation
from tests.factories import add_entries, create_customer_account, create_transaction


@pytest.fixture
async def posted(db_session: AsyncSession) -> tuple[Transaction, list[LedgerEntry]]:
    payer = await create_customer_account(db_session, balance_minor=5000)
    payee = await create_customer_account(db_session)
    transaction = await create_transaction(db_session, kind=TransactionKind.TRANSFER)
    entries = add_entries(db_session, transaction, (payer, -3000), (payee, 3000))
    await check_deferred_constraints(db_session)
    return transaction, entries


async def test_transaction_cannot_be_updated(
    db_session: AsyncSession, posted: tuple[Transaction, list[LedgerEntry]]
) -> None:
    transaction, _ = posted

    async with raises_violation("transactions_immutable"):
        await db_session.execute(
            update(Transaction)
            .where(Transaction.id == transaction.id)
            .values(kind=TransactionKind.DEPOSIT)
        )


async def test_transaction_cannot_be_deleted(
    db_session: AsyncSession, posted: tuple[Transaction, list[LedgerEntry]]
) -> None:
    transaction, _ = posted

    async with raises_violation("transactions_immutable"):
        await db_session.execute(delete(Transaction).where(Transaction.id == transaction.id))


async def test_ledger_entry_amount_cannot_be_updated(
    db_session: AsyncSession, posted: tuple[Transaction, list[LedgerEntry]]
) -> None:
    _, entries = posted

    # Even a change that keeps the transaction balanced (both legs scaled) is forbidden.
    async with raises_violation("ledger_entries_immutable"):
        await db_session.execute(
            update(LedgerEntry)
            .where(LedgerEntry.id.in_([entry.id for entry in entries]))
            .values(amount_minor=LedgerEntry.amount_minor * 2)
        )


async def test_ledger_entry_cannot_be_deleted(
    db_session: AsyncSession, posted: tuple[Transaction, list[LedgerEntry]]
) -> None:
    _, entries = posted

    async with raises_violation("ledger_entries_immutable"):
        await db_session.execute(delete(LedgerEntry).where(LedgerEntry.id == entries[0].id))


async def test_account_balance_projection_remains_updatable(
    db_session: AsyncSession, posted: tuple[Transaction, list[LedgerEntry]]
) -> None:
    # Accounts are deliberately outside the immutability boundary: the posting service updates
    # balance_minor under a row lock (ADR 0005).
    _, entries = posted
    payer_id = entries[0].account_id

    await db_session.execute(
        update(Account)
        .where(Account.id == payer_id)
        .values(balance_minor=Account.balance_minor - 3000)
    )

    assert (
        await db_session.scalar(select(Account.balance_minor).where(Account.id == payer_id)) == 2000
    )
