"""Plain async helpers that insert synthetic rows for database tests.

Phase 2 has no posting service yet, so tests write rows (including balance_minor) directly.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import Account, Ledger, LedgerEntry, Transaction, User
from ledger_api.domain.account import AccountKind
from ledger_api.domain.currency import Currency
from ledger_api.domain.transaction import TransactionKind


async def get_ledger(session: AsyncSession, currency: Currency) -> Ledger:
    return (await session.scalars(select(Ledger).where(Ledger.currency == currency))).one()


async def get_settlement_account(session: AsyncSession, currency: Currency) -> Account:
    ledger = await get_ledger(session, currency)
    statement = select(Account).where(
        Account.ledger_id == ledger.id, Account.kind == AccountKind.SYSTEM
    )
    return (await session.scalars(statement)).one()


async def create_user(session: AsyncSession, email: str | None = None) -> User:
    user = User(email=email or f"user-{uuid.uuid7().hex}@example.test")
    session.add(user)
    await session.flush()
    return user


async def create_customer_account(
    session: AsyncSession,
    currency: Currency = Currency.GBP,
    *,
    user: User | None = None,
    balance_minor: int = 0,
) -> Account:
    owner = user or await create_user(session)
    ledger = await get_ledger(session, currency)
    account = Account(
        ledger_id=ledger.id,
        user_id=owner.id,
        kind=AccountKind.CUSTOMER,
        balance_minor=balance_minor,
    )
    session.add(account)
    await session.flush()
    return account


async def create_transaction(
    session: AsyncSession,
    currency: Currency = Currency.GBP,
    kind: TransactionKind = TransactionKind.TRANSFER,
) -> Transaction:
    ledger = await get_ledger(session, currency)
    transaction = Transaction(ledger_id=ledger.id, kind=kind)
    session.add(transaction)
    await session.flush()
    return transaction


def add_entries(
    session: AsyncSession, transaction: Transaction, *legs: tuple[Account, int]
) -> list[LedgerEntry]:
    """Add one entry per (account, amount_minor) leg. The entry takes the transaction's ledger."""
    entries = [
        LedgerEntry(
            transaction_id=transaction.id,
            account_id=account.id,
            ledger_id=transaction.ledger_id,
            amount_minor=amount_minor,
        )
        for account, amount_minor in legs
    ]
    session.add_all(entries)
    return entries
