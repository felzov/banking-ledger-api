"""Credential and session persistence (ADR 0013). Callers own the transaction; nothing here
commits, hashes or logs.

Session times are always the database's: created_at defaults to now(), and expiry and
liveness are compared with now() in SQL, so application clocks never matter.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import AuthSession, User, UserCredential


@dataclass(frozen=True, slots=True)
class LoginCandidate:
    """The user an email belongs to, and their password hash if they have registered one."""

    user_id: uuid.UUID
    password_hash: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class StoredSession:
    session_id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime


async def find_login_candidate(session: AsyncSession, *, email: str) -> LoginCandidate | None:
    """The user registered with `email` (canonical form), or None."""
    row = (
        await session.execute(
            select(User.id, UserCredential.password_hash)
            .outerjoin(UserCredential, UserCredential.user_id == User.id)
            .where(User.email == email)
        )
    ).one_or_none()
    return None if row is None else LoginCandidate(row.id, row.password_hash)


async def insert_credential(
    session: AsyncSession, *, user_id: uuid.UUID, password_hash: str
) -> None:
    await session.execute(
        insert(UserCredential).values(user_id=user_id, password_hash=password_hash)
    )


async def replace_password_hash(
    session: AsyncSession, *, user_id: uuid.UUID, expected: str, replacement: str
) -> bool:
    """Swap a hash for a rehashed one, only if it is still `expected` (a concurrent change
    wins). Returns whether a row changed."""
    result = await session.execute(
        update(UserCredential)
        .where(UserCredential.user_id == user_id, UserCredential.password_hash == expected)
        .values(password_hash=replacement, updated_at=func.now())
        .returning(UserCredential.user_id)
    )
    return result.first() is not None


async def insert_session(
    session: AsyncSession, *, user_id: uuid.UUID, token_hash: bytes, lifetime: timedelta
) -> StoredSession:
    """A session expiring `lifetime` after the database's now()."""
    row = (
        await session.execute(
            insert(AuthSession)
            .values(user_id=user_id, token_hash=token_hash, expires_at=func.now() + lifetime)
            .returning(AuthSession.id, AuthSession.user_id, AuthSession.expires_at)
        )
    ).one()
    return StoredSession(row.id, row.user_id, row.expires_at)


async def revoke_live_session(session: AsyncSession, *, token_hash: bytes) -> StoredSession | None:
    """Revoke the live (unrevoked, unexpired) session with this token digest, or return None.

    Matching only live sessions is what keeps a repeated logout from ever reaching the
    revoke-only trigger: an already revoked or expired session matches no row. Two concurrent
    logouts serialize on the row lock; the second re-evaluates the condition and matches none.
    """
    row = (
        await session.execute(
            update(AuthSession)
            .where(
                AuthSession.token_hash == token_hash,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > func.now(),
            )
            .values(revoked_at=func.now())
            .returning(AuthSession.id, AuthSession.user_id, AuthSession.expires_at)
        )
    ).one_or_none()
    return None if row is None else StoredSession(row.id, row.user_id, row.expires_at)
