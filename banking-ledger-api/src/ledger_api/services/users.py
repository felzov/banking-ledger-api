import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.errors import violated_constraint
from ledger_api.data.models import User
from ledger_api.domain.errors import EmailAlreadyRegisteredError, UserNotFoundError

UNIQUE_EMAIL = "uq_users_email"


async def register_user(session: AsyncSession, *, email: str) -> User:
    """Create a user. `email` must already be canonical (see domain.user.normalize_email).

    Uniqueness is enforced by the database, not by a SELECT beforehand: a check-then-insert
    would let two concurrent registrations of the same address both pass the check.
    """
    try:
        async with session.begin():
            user = User(email=email)
            session.add(user)
            await session.flush()  # INSERT ... RETURNING: violations surface here
    except IntegrityError as error:
        # session.begin() has already rolled back. Only the known rule is translated; any
        # other violation is a bug and propagates.
        if violated_constraint(error) == UNIQUE_EMAIL:
            raise EmailAlreadyRegisteredError from error
        raise
    return user


async def get_user(session: AsyncSession, *, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise UserNotFoundError
    return user
