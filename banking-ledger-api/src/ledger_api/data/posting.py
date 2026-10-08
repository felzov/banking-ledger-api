"""Persistence steps of a posting. Callers own the transaction; nothing here commits.

Locking protocol (ADR 0005, ADR 0010):
- Before locking, only immutable facts may be read (which ledger and settlement account an
  account belongs to). Mutable state (balances) is read only from the locking statement.
- Every account a posting touches is locked in ONE statement, in ascending id order, so all
  postings acquire locks in the same global order and cannot deadlock with each other.
- Locked state is returned as plain rows, never ORM entities: an Account already in the
  session's identity map would keep its stale balance even after a fresh locking SELECT.
"""

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ledger_api.data.models import Account, LedgerEntry, Transaction
from ledger_api.domain.account import AccountKind
from ledger_api.domain.posting import Posting


@dataclass(frozen=True, slots=True)
class LockedAccount:
    """An account row as read by the locking statement: current and locked until COMMIT."""

    id: uuid.UUID
    user_id: uuid.UUID | None
    kind: AccountKind
    ledger_id: uuid.UUID
    balance_minor: int


async def find_settlement_account_id(
    session: AsyncSession, *, account_id: uuid.UUID
) -> uuid.UUID | None:
    """The settlement account of customer account `account_id`'s ledger, or None if there is
    no such customer account (it does not exist, or is itself a system account).

    Reads immutable facts only (an account's kind and ledger, and a ledger's settlement
    account, never change), so it is safe before locking.
    """
    customer, settlement = aliased(Account), aliased(Account)
    statement = (
        select(settlement.id)
        .join(customer, customer.ledger_id == settlement.ledger_id)
        .where(
            customer.id == account_id,
            customer.kind == AccountKind.CUSTOMER,
            settlement.kind == AccountKind.SYSTEM,
        )
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def lock_accounts(
    session: AsyncSession, account_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, LockedAccount]:
    """Lock the given accounts (one statement, ascending id) and return their current state.

    Missing accounts are simply absent from the result. Waits at most lock_timeout.
    """
    statement = (
        select(Account.id, Account.user_id, Account.kind, Account.ledger_id, Account.balance_minor)
        .where(Account.id.in_(sorted(account_ids)))
        .order_by(Account.id)
        # OF accounts: lock account rows only. Joining another table into a FOR UPDATE would
        # lock its rows too (e.g. a ledger row, serializing every posting in a currency).
        .with_for_update(of=Account)
    )
    rows = (await session.execute(statement)).all()
    return {
        row.id: LockedAccount(row.id, row.user_id, row.kind, row.ledger_id, row.balance_minor)
        for row in rows
    }


async def insert_posting(
    session: AsyncSession, posting: Posting, *, ledger_id: uuid.UUID
) -> Transaction:
    """Insert the transaction header, sealed with its entry count, and its entries.

    The balance and entry-count checks run at COMMIT (deferred triggers).
    """
    transaction = Transaction(
        ledger_id=ledger_id, kind=posting.kind, entry_count=len(posting.lines)
    )
    session.add(transaction)
    await session.flush()  # the header must exist before entries reference it
    await session.execute(
        insert(LedgerEntry),
        [
            {
                "transaction_id": transaction.id,
                "account_id": line.account_id,
                "ledger_id": ledger_id,
                "amount_minor": line.amount_minor,
            }
            for line in posting.lines
        ],
    )
    return transaction


async def apply_balance_deltas(
    session: AsyncSession, deltas: Mapping[uuid.UUID, int]
) -> dict[uuid.UUID, int]:
    """Apply relative balance changes (balance = balance + delta) and return the new balances.

    Relative updates stay correct even if a caller forgot to lock; the customer balance
    CHECK constraint is the last line of defense against overdrafts (ADR 0005).
    """
    balances: dict[uuid.UUID, int] = {}
    for account_id in sorted(deltas):
        statement = (
            update(Account)
            .where(Account.id == account_id)
            .values(balance_minor=Account.balance_minor + deltas[account_id])
            .returning(Account.balance_minor)
            # Plain SQL semantics: never touch ORM objects in the session.
            .execution_options(synchronize_session=False)
        )
        balances[account_id] = (await session.execute(statement)).scalar_one()
    return balances
