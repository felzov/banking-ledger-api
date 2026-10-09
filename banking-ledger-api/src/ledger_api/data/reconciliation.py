"""Ledger and audit consistency checks (ADR 0003, ADR 0011, ADR 0012). Read-only: they report,
they never repair.

Each check recomputes its invariant from the raw rows. None of them trusts the mechanism that
normally enforces it: triggers and foreign keys can be disabled by a superuser or the table
owner (the application role currently is one), and balance_minor is maintained by application
code only. Used by the tests today; ready for a scheduled job.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import Select, and_, cast, func, literal, or_, select
from sqlalchemy import Uuid as SqlUuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ledger_api.data.models import Account, AuditEvent, LedgerEntry, Transaction
from ledger_api.domain.audit import AuditOutcome


@dataclass(frozen=True, slots=True)
class BalanceMismatch:
    account_id: uuid.UUID
    balance_minor: int
    entries_total_minor: int


@dataclass(frozen=True, slots=True)
class InvalidTransaction:
    transaction_id: uuid.UUID
    declared_entry_count: int
    entry_count: int
    entries_total_minor: int


async def find_balance_mismatches(
    session: AsyncSession, *, account_ids: Collection[uuid.UUID] | None = None
) -> list[BalanceMismatch]:
    """Accounts whose balance_minor differs from the sum of their ledger entries."""
    entries_total = func.coalesce(func.sum(LedgerEntry.amount_minor), 0)
    statement = (
        select(Account.id, Account.balance_minor, entries_total)
        .outerjoin(LedgerEntry, LedgerEntry.account_id == Account.id)
        .group_by(Account.id)
        .having(Account.balance_minor != entries_total)
    )
    if account_ids is not None:
        statement = statement.where(Account.id.in_(account_ids))
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


async def find_invalid_transactions(session: AsyncSession) -> list[InvalidTransaction]:
    """Transactions with fewer than two entries, entries that do not sum to zero, or a number
    of entries other than the declared entry_count: the deferred trigger's rules, re-checked."""
    entry_count = func.count(LedgerEntry.id)
    entries_total = func.coalesce(func.sum(LedgerEntry.amount_minor), 0)
    statement = (
        select(Transaction.id, Transaction.entry_count, entry_count, entries_total)
        .outerjoin(LedgerEntry, LedgerEntry.transaction_id == Transaction.id)
        .group_by(Transaction.id)
        .having(or_(entry_count < 2, entries_total != 0, entry_count != Transaction.entry_count))
    )
    return [
        InvalidTransaction(row[0], row[1], row[2], int(row[3]))
        for row in (await session.execute(statement)).all()
    ]


async def find_misplaced_entries(session: AsyncSession) -> list[uuid.UUID]:
    """Entries whose account or transaction is missing, or lives in another ledger: what the
    composite foreign keys forbid, re-checked."""
    statement = (
        select(LedgerEntry.id)
        .outerjoin(Account, Account.id == LedgerEntry.account_id)
        .outerjoin(Transaction, Transaction.id == LedgerEntry.transaction_id)
        .where(
            or_(
                Account.id.is_(None),
                Transaction.id.is_(None),
                Account.ledger_id != LedgerEntry.ledger_id,
                Transaction.ledger_id != LedgerEntry.ledger_id,
            )
        )
        .order_by(LedgerEntry.id)
    )
    return list((await session.scalars(statement)).all())


async def find_posted_transactions_without_success_audit(
    session: AsyncSession, *, transaction_ids: Collection[uuid.UUID] | None = None
) -> list[uuid.UUID]:
    """Posted transactions that lack their succeeded audit event, or whose event names another
    kind of posting (posting.<kind> must match the transaction's kind).

    Filterable: transactions written without the services (tests that exercise the schema
    directly) legitimately have no event.
    """
    expected_action = literal("posting.") + Transaction.kind
    audited = aliased(AuditEvent)
    statement: Select[uuid.UUID] = (
        select(Transaction.id)
        .outerjoin(
            audited,
            and_(
                audited.transaction_id == Transaction.id,
                audited.outcome == AuditOutcome.SUCCEEDED,
                audited.action == expected_action,
            ),
        )
        .where(audited.id.is_(None))
        .order_by(Transaction.id)
    )
    if transaction_ids is not None:
        statement = statement.where(Transaction.id.in_(transaction_ids))
    return list((await session.scalars(statement)).all())


async def find_failed_events_for_committed_transactions(session: AsyncSession) -> list[int]:
    """Failed events whose attempted transaction exists after all: the COMMIT succeeded but the
    caller saw an error (e.g. the connection dropped during COMMIT). The posting stands and has
    its succeeded event; the failed event records what the caller was told."""
    attempted = cast(AuditEvent.details["attempted_transaction_id"].astext, SqlUuid)
    statement = (
        select(AuditEvent.id)
        .join(Transaction, Transaction.id == attempted)
        .where(
            AuditEvent.outcome == AuditOutcome.FAILED,
            AuditEvent.details.has_key("attempted_transaction_id"),
        )
        .order_by(AuditEvent.id)
    )
    return list((await session.scalars(statement)).all())
