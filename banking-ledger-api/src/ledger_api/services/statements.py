"""Account statements: an owner's entries, newest first, with running balances (ADR 0012).

Service only in Phase 5; the HTTP endpoint arrives with the financial API in Phase 7.

Pagination is keyset-based. The cursor is the entry id of the last row of the previous page;
the next page continues with strictly older entries (lower sequence_number). Because an
account's committed entries always form a prefix of its history in sequence order (a later
commit always draws a higher number), postings committed between two page requests only ever
appear above page 1: following the cursor never repeats or skips an entry, and every entry's
running balance is the same whichever request shows it. New postings show up on a fresh first
page.

A statement is checked before it is returned: the account's balance must equal the sum of its
entries. If it does not, the service fails closed (StatementInconsistencyError) and logs an
ERROR. A possibly wrong statement is never returned as if it were valid.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.accounts import get_owned_account
from ledger_api.data.statements import find_cursor_position, read_statement
from ledger_api.domain.errors import (
    AccountNotFoundError,
    InvalidStatementCursorError,
    InvalidStatementLimitError,
    StatementInconsistencyError,
)
from ledger_api.domain.transaction import TransactionKind

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class StatementEntry:
    entry_id: uuid.UUID
    transaction_id: uuid.UUID
    kind: TransactionKind
    # Signed: negative debits the account, positive credits it.
    amount_minor: int
    # The account's balance right after this entry: computed by the query, never stored.
    balance_after_minor: int
    created_at: datetime
    # Deliberately no counterparty: a statement reveals only the owner's own account (ADR 0010).


@dataclass(frozen=True, slots=True)
class StatementPage:
    account_id: uuid.UUID
    # The current balance, from the same snapshot as the entries.
    balance_minor: int
    entries: tuple[StatementEntry, ...]
    # Pass as `cursor` to get the next (older) page; None on the last page.
    next_cursor: uuid.UUID | None


async def get_statement(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    limit: int = DEFAULT_LIMIT,
    cursor: uuid.UUID | None = None,
) -> StatementPage:
    # type() rather than isinstance(): bool is a subclass of int, and True is not a page size.
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise InvalidStatementLimitError
    if await get_owned_account(session, owner_id=owner_id, account_id=account_id) is None:
        # Missing, someone else's, or a system account: indistinguishable (ADR 0009).
        raise AccountNotFoundError

    before = None
    if cursor is not None:
        before = await find_cursor_position(session, account_id=account_id, entry_id=cursor)
        if before is None:
            raise InvalidStatementCursorError

    snapshot = await read_statement(session, account_id=account_id, before=before, limit=limit)
    if snapshot.balance_minor != snapshot.entries_total_minor:
        logger.error(
            "statement refused: account %s has balance_minor=%d but its entries sum to %d",
            account_id,
            snapshot.balance_minor,
            snapshot.entries_total_minor,
        )
        raise StatementInconsistencyError

    rows = snapshot.rows[:limit]
    has_more = len(snapshot.rows) > limit
    return StatementPage(
        account_id=account_id,
        balance_minor=snapshot.balance_minor,
        entries=tuple(
            StatementEntry(
                entry_id=row.entry_id,
                transaction_id=row.transaction_id,
                kind=row.kind,
                amount_minor=row.amount_minor,
                balance_after_minor=row.balance_after_minor,
                created_at=row.created_at,
            )
            for row in rows
        ),
        next_cursor=rows[-1].entry_id if has_more else None,
    )
