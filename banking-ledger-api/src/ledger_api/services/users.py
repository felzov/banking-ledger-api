import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.errors import violated_constraint
from ledger_api.data.models import User
from ledger_api.domain.audit import AuditAction
from ledger_api.domain.errors import EmailAlreadyRegisteredError, UserNotFoundError
from ledger_api.services.audit import AuditContext, audited, record_in_transaction

UNIQUE_EMAIL = "uq_users_email"


async def register_user(session: AsyncSession, *, email: str) -> User:
    """Create a user. `email` must already be canonical (see domain.user.normalize_email).

    Uniqueness is enforced by the database, not by a SELECT beforehand: a check-then-insert
    would let two concurrent registrations of the same address both pass the check.

    Audited as user.register (ADR 0011). The email address is never part of the event: the
    audit trail records that a registration happened, not personal data.
    """
    context = AuditContext(AuditAction.USER_REGISTER, actor_user_id=None)

    async def run() -> User:
        try:
            async with session.begin():
                user = User(email=email)
                session.add(user)
                await session.flush()  # INSERT ... RETURNING: violations surface here
                await record_in_transaction(session, context.succeeded(actor_user_id=user.id))
        except IntegrityError as error:
            # session.begin() has already rolled back. Only the known rule is translated; any
            # other violation is a bug and propagates.
            if violated_constraint(error) == UNIQUE_EMAIL:
                raise EmailAlreadyRegisteredError from error
            raise
        return user

    return await audited(session, context, run)


async def get_user(session: AsyncSession, *, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise UserNotFoundError
    return user
