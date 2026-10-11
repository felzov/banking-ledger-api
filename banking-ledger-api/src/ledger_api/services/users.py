import asyncio
import uuid

from argon2 import PasswordHasher
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.auth import insert_credential
from ledger_api.data.errors import violated_constraint
from ledger_api.data.models import User
from ledger_api.data.users import find_user
from ledger_api.domain.audit import AuditAction
from ledger_api.domain.errors import EmailAlreadyRegisteredError, UserNotFoundError
from ledger_api.domain.passwords import hash_password
from ledger_api.services.audit import AuditContext, audited, record_in_transaction

UNIQUE_EMAIL = "uq_users_email"


async def register_user_with_password(
    session: AsyncSession, hasher: PasswordHasher, *, email: str, password: str
) -> User:
    """Register a user who can log in (ADR 0013). The API's registration.

    The password is checked against the policy and hashed with Argon2id in a worker thread
    BEFORE any transaction begins: the slow hash never runs on the event loop and never holds
    a database connection. The user, their credential and the audit event then commit
    together, or not at all.
    """
    password_hash = await asyncio.to_thread(hash_password, hasher, password)
    return await register_user(session, email=email, password_hash=password_hash)


async def register_user(
    session: AsyncSession, *, email: str, password_hash: str | None = None
) -> User:
    """Create a user. `email` must already be canonical (see domain.user.normalize_email).

    `password_hash`, an Argon2id hash, is stored as the user's credential in the same
    transaction. Without one the user cannot log in (internal callers, e.g. test factories).

    Uniqueness is enforced by the database, not by a SELECT beforehand: a check-then-insert
    would let two concurrent registrations of the same address both pass the check.

    Audited as user.register (ADR 0011). The email address and the password (hash) are never
    part of the event: the audit trail records that a registration happened, not personal data.
    """
    context = AuditContext(AuditAction.USER_REGISTER, actor_user_id=None)

    async def run() -> User:
        try:
            async with session.begin():
                user = User(email=email)
                session.add(user)
                await session.flush()  # INSERT ... RETURNING: violations surface here
                if password_hash is not None:
                    await insert_credential(session, user_id=user.id, password_hash=password_hash)
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
    # Reads own their transaction like writes do: the session is returned outside any
    # transaction, with its connection back in the pool, ready for the next service call.
    async with session.begin():
        user = await find_user(session, user_id=user_id)
    if user is None:
        raise UserNotFoundError
    return user
