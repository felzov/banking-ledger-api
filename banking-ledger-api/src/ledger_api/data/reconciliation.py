"""Ledger consistency checks (ADR 0003). Read-only: they report, they never repair.

balance_minor is an application-maintained projection, so it is not a database constraint
(that would cost O(entries) per commit). These queries verify it, and the per-ledger
zero-sum, across the whole database. Used by the tests today; ready for a scheduled job.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import Account, LedgerEntry


@dataclass(frozen=True, slots=True)
class BalanceMismatch:
    account_id: uuid.UUID
    balance_minor: int
    entries_total_minor: int


async def find_balance_mismatches(session: AsyncSession) -> list[BalanceMismatch]:
    """Accounts whose balance_minor differs from the sum of their ledger entries."""
    entries_total = func.coalesce(func.sum(LedgerEntry.amount_minor), 0)
    statement = (
        select(Account.id, Account.balance_minor, entries_total)
        .outerjoin(LedgerEntry, LedgerEntry.account_id == Account.id)
        .group_by(Account.id)
        .having(Account.balance_minor != entries_total)
    )
    return [BalanceMismatch(*row) for row in (await session.execute(statement)).all()]


async def find_unbalanced_ledgers(session: AsyncSession) -> dict[uuid.UUID, int]:
    """Ledgers whose entries do not sum to zero (impossible if every transaction balances)."""
    total = func.sum(LedgerEntry.amount_minor)
    statement = (
        select(LedgerEntry.ledger_id, total).group_by(LedgerEntry.ledger_id).having(total != 0)
    )
    return {row[0]: int(row[1]) for row in (await session.execute(statement)).all()}


async def find_ledgers_with_nonzero_balances(session: AsyncSession) -> dict[uuid.UUID, int]:
    """Ledgers whose account balances do not sum to zero, i.e. whose settlement account does
    not mirror the customers' money (settlement = -sum(customer balances))."""
    total = func.sum(Account.balance_minor)
    statement = select(Account.ledger_id, total).group_by(Account.ledger_id).having(total != 0)
    return {row[0]: int(row[1]) for row in (await session.execute(statement)).all()}
