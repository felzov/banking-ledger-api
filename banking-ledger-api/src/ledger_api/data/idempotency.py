"""Idempotency key persistence (ADR 0006). Callers own the transaction; nothing here commits.

The claim is the FIRST statement of a posting transaction, before any account lock: the global
lock order is "key, then accounts", and a request waiting on a key holds no account lock, so
keys add no deadlock cycle to the posting protocol (ADR 0010).

Replays are rebuilt from immutable ledger rows, never from accounts.balance_minor: the balance
after the original entry is the sum of the account's entries up to and including it in
sequence order, the same calculation statements use (ADR 0012).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ledger_api.data.models import IdempotencyKey, LedgerEntry, Transaction
from ledger_api.domain.transaction import TransactionKind


@dataclass(frozen=True, slots=True)
class StoredKey:
    """A key consumed by a committed posting."""

    request_fingerprint: bytes
    transaction_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class PostedEntry:
    """One account's side of a committed transaction, as it was when it was posted."""

    transaction_id: uuid.UUID
    kind: TransactionKind
    created_at: datetime
    amount_minor: int
    # The account's balance right after this entry: computed from the entries, never stored.
    balance_after_minor: int


async def claim_idempotency_key(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    key: str,
    request_fingerprint: bytes,
    transaction_id: uuid.UUID,
) -> StoredKey | None:
    """Claim `key` for this transaction. Returns None if the claim succeeded, otherwise the key
    as stored by the committed posting that consumed it first.

    INSERT ... ON CONFLICT DO NOTHING never raises a unique violation, so the transaction is
    never left aborted. If another transaction holds an uncommitted claim on the same key, the
    INSERT waits for it to end (at most lock_timeout): if it commits, the key is taken; if it
    rolls back, this claim proceeds. That wait is also what resolves a posting whose COMMIT is
    still in progress, which an immediate SELECT could not: it would see no row yet.

    The referenced transaction does not exist yet: the foreign key is checked at COMMIT. An
    unknown user_id raises IntegrityError (fk_idempotency_keys_user_id_users).
    """
    claim = (
        insert(IdempotencyKey)
        .values(
            user_id=user_id,
            key=key,
            request_fingerprint=request_fingerprint,
            transaction_id=transaction_id,
        )
        .on_conflict_do_nothing(index_elements=[IdempotencyKey.user_id, IdempotencyKey.key])
        .returning(IdempotencyKey.transaction_id)
    )
    if (await session.execute(claim)).scalar_one_or_none() is not None:
        return None

    # A new statement, so a new READ COMMITTED snapshot: it sees the row the conflict was with,
    # which is committed (the INSERT waited for it) and never deleted (append-only).
    stored = (
        await session.execute(
            select(IdempotencyKey.request_fingerprint, IdempotencyKey.transaction_id).where(
                IdempotencyKey.user_id == user_id, IdempotencyKey.key == key
            )
        )
    ).one_or_none()
    if stored is None:
        raise RuntimeError("idempotency key conflicted but no committed row is visible")
    return StoredKey(bytes(stored.request_fingerprint), stored.transaction_id)


async def find_posted_entry(
    session: AsyncSession, *, transaction_id: uuid.UUID, account_id: uuid.UUID
) -> PostedEntry | None:
    """`account_id`'s entry in committed transaction `transaction_id`, with the balance right
    after it, or None if the transaction has no entry on that account.

    Entries are immutable and an account's committed history only grows at the end (ADR 0012),
    so the result is the same whenever it is read: later postings never change it.
    """
    entry, history = aliased(LedgerEntry), aliased(LedgerEntry)
    balance_after = (
        select(func.sum(history.amount_minor))
        .where(
            history.account_id == entry.account_id,
            history.sequence_number <= entry.sequence_number,
        )
        .scalar_subquery()
    )
    statement = (
        select(
            Transaction.id,
            Transaction.kind,
            Transaction.created_at,
            entry.amount_minor,
            balance_after.label("balance_after_minor"),
        )
        .join(entry, entry.transaction_id == Transaction.id)
        .where(Transaction.id == transaction_id, entry.account_id == account_id)
    )
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None
    return PostedEntry(
        transaction_id=row.id,
        kind=TransactionKind(row.kind),
        created_at=row.created_at,
        amount_minor=row.amount_minor,
        balance_after_minor=int(row.balance_after_minor),
    )
