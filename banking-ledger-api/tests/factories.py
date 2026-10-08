"""Plain async helpers that create synthetic data for database tests.

Most helpers write rows directly (including balance_minor), for tests that exercise the
schema itself inside a rolled-back transaction. commit_funded_account goes through the
services instead, for tests that COMMIT and must leave the database reconciled.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledger_api.data.models import Account, Ledger, LedgerEntry, Transaction, User
from ledger_api.domain.account import AccountKind
from ledger_api.domain.currency import Currency
from ledger_api.domain.transaction import TransactionKind
from ledger_api.services.accounts import open_customer_account
from ledger_api.services.posting import deposit
from ledger_api.services.users import register_user


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
    entry_count: int = 2,
) -> Transaction:
    ledger = await get_ledger(session, currency)
    transaction = Transaction(ledger_id=ledger.id, kind=kind, entry_count=entry_count)
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


@dataclass(frozen=True)
class FundedAccount:
    owner_id: uuid.UUID
    account_id: uuid.UUID


async def commit_funded_account(
    sessionmaker: async_sessionmaker[AsyncSession],
    amount_minor: int,
    currency: Currency = Currency.GBP,
) -> FundedAccount:
    """A committed user and account, funded through a real deposit.

    For tests that COMMIT: funding goes through the posting service, so the shared test
    database stays reconciled.
    """
    async with sessionmaker() as session:
        user = await register_user(session, email=f"funded-{uuid.uuid7().hex}@example.com")
    async with sessionmaker() as session:
        account = await open_customer_account(session, owner_id=user.id, currency=currency)
    if amount_minor:
        async with sessionmaker() as session:
            await deposit(
                session, owner_id=user.id, account_id=account.id, amount_minor=amount_minor
            )
    return FundedAccount(user.id, account.id)
