"""User queries. Callers own the transaction; nothing here commits."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import User


async def find_user(session: AsyncSession, *, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)
