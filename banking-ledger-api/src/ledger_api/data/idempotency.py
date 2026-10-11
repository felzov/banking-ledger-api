"""Idempotency key persistence (ADR 0006). Callers own the transaction; nothing here commits.

The claim is the FIRST statement of a posting transaction, before any account lock: the global
lock order is "key, then accounts", and a request waiting on a key holds no account lock, so
keys add no deadlock cycle to the posting protocol (ADR 0010).

Replays are rebuilt from immutable ledger rows (data.statements.find_posted_entry), never from
accounts.balance_minor.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import IdempotencyKey


@dataclass(frozen=True, slots=True)
class StoredKey:
    """A key consumed by a committed posting."""

    request_fingerprint: bytes
    transaction_id: uuid.UUID


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
