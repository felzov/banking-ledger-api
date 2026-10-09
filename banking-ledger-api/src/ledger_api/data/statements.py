"""Account statement queries (ADR 0012). Read-only; callers check ownership first.

Order: ledger_entries.sequence_number, which for one account is commit order. Running balances
are computed here, never stored: SUM(amount_minor) over every entry up to and including the
row, in sequence order.

Snapshot: a page, its running balances, the account's balance_minor and the total of all its
entries come from ONE SQL statement, i.e. one READ COMMITTED snapshot. A posting writes its
entries and its balance update in one transaction, so they are always seen together.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import Account, LedgerEntry, Transaction
from ledger_api.domain.transaction import TransactionKind


@dataclass(frozen=True, slots=True)
class StatementRow:
    entry_id: uuid.UUID
    transaction_id: uuid.UUID
    kind: TransactionKind
    amount_minor: int
    balance_after_minor: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StatementSnapshot:
    balance_minor: int
    # SUM of every entry of the account: must equal balance_minor (the self-check).
    entries_total_minor: int
    # Newest first, at most limit + 1 rows: the extra row only signals a next page.
    rows: list[StatementRow]


async def find_cursor_position(
    session: AsyncSession, *, account_id: uuid.UUID, entry_id: uuid.UUID
) -> int | None:
    """The sequence number of `entry_id` if it is an entry of `account_id`, else None.

    Entries and their numbers never change, so resolving the cursor in its own statement
    cannot disagree with the page query that follows.
    """
    statement = select(LedgerEntry.sequence_number).where(
        LedgerEntry.account_id == account_id, LedgerEntry.id == entry_id
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def read_statement(
    session: AsyncSession, *, account_id: uuid.UUID, before: int | None, limit: int
) -> StatementSnapshot:
    """Up to limit + 1 entries older than sequence number `before` (newest first if None)."""
    history = (
        select(
            LedgerEntry.id.label("entry_id"),
            LedgerEntry.transaction_id,
            LedgerEntry.amount_minor,
            LedgerEntry.sequence_number,
            Transaction.kind,
            Transaction.created_at,
            func.sum(LedgerEntry.amount_minor)
            .over(order_by=LedgerEntry.sequence_number)
            .label("balance_after_minor"),
        )
        .join(Transaction, Transaction.id == LedgerEntry.transaction_id)
        .where(LedgerEntry.account_id == account_id)
        .cte("history")
    )
    # The window runs over the whole history (in the CTE); the cursor only filters its output,
    # so the running balance of a row is the same on every page.
    page_query = select(history)
    if before is not None:
        page_query = page_query.where(history.c.sequence_number < before)
    page = page_query.order_by(history.c.sequence_number.desc()).limit(limit + 1).subquery("page")
    entries_total = (
        select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0))
        .where(LedgerEntry.account_id == account_id)
        .scalar_subquery()
    )
    statement = (
        select(Account.balance_minor, entries_total.label("entries_total_minor"), page)
        .select_from(Account)
        .outerjoin(page, true())  # an account without entries still yields its balance
        .where(Account.id == account_id)
        .order_by(page.c.sequence_number.desc())
    )
    result = (await session.execute(statement)).all()
    if not result:
        raise LookupError("read_statement needs an existing account; check ownership first")
    first = result[0]
    rows = [
        StatementRow(
            entry_id=row.entry_id,
            transaction_id=row.transaction_id,
            kind=TransactionKind(row.kind),
            amount_minor=row.amount_minor,
            balance_after_minor=int(row.balance_after_minor),
            created_at=row.created_at,
        )
        for row in result
        if row.entry_id is not None
    ]
    return StatementSnapshot(first.balance_minor, int(first.entries_total_minor), rows)
