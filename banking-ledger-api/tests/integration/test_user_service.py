import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.errors import violated_constraint
from ledger_api.data.models import User
from ledger_api.domain.errors import EmailAlreadyRegisteredError, UserNotFoundError
from ledger_api.services.users import get_user, register_user
from tests.conftest import SessionFactory


def _unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid7().hex}@example.com"


async def test_register_user_persists_the_user(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    email = _unique_email()

    user = await register_user(new_session(), email=email)

    # id and created_at come back from the INSERT (UUIDv7 + server-side now()).
    assert user.id.version == 7
    assert user.created_at.tzinfo is not None
    stored = await db_session.get(User, user.id)
    assert stored is not None
    assert stored.email == email


async def test_duplicate_email_is_rejected(new_session: SessionFactory) -> None:
    email = _unique_email()
    await register_user(new_session(), email=email)

    with pytest.raises(EmailAlreadyRegisteredError):
        await register_user(new_session(), email=email)


async def test_only_the_unique_email_violation_is_translated(new_session: SessionFactory) -> None:
    # A non-canonical email breaks the service's precondition. The database rejects it, and
    # the service must not disguise that bug as "already registered".
    with pytest.raises(IntegrityError) as excinfo:
        await register_user(new_session(), email="Not-Canonical@example.com")

    assert violated_constraint(excinfo.value) == "ck_users_email_lowercase"


async def test_get_user_returns_the_user(new_session: SessionFactory) -> None:
    created = await register_user(new_session(), email=_unique_email())

    found = await get_user(new_session(), user_id=created.id)

    assert (found.id, found.email) == (created.id, created.email)


async def test_get_user_raises_for_an_unknown_id(new_session: SessionFactory) -> None:
    with pytest.raises(UserNotFoundError):
        await get_user(new_session(), user_id=uuid.uuid7())


@pytest.mark.concurrency
async def test_concurrent_registrations_of_one_email_create_exactly_one_user(
    engine: AsyncEngine,
) -> None:
    # Real sessions and real COMMITs on separate connections: the unique index, not a
    # SELECT-before-INSERT, decides the race. (Leaves one user behind in the disposable test DB.)
    sessionmaker = create_sessionmaker(engine)
    email = _unique_email("race")
    attempts = 5

    async def attempt() -> User:
        async with sessionmaker() as session:
            return await register_user(session, email=email)

    results = await asyncio.gather(*(attempt() for _ in range(attempts)), return_exceptions=True)

    created = [result for result in results if isinstance(result, User)]
    rejected = [result for result in results if isinstance(result, EmailAlreadyRegisteredError)]
    assert len(created) == 1
    assert len(rejected) == attempts - 1
    async with sessionmaker() as session:
        count = await session.scalar(select(func.count()).where(User.email == email))
    assert count == 1
