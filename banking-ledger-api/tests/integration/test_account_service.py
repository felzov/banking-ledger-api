import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.models import Account
from ledger_api.domain.account import AccountKind
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import (
    AccountAlreadyExistsError,
    AccountNotFoundError,
    UserNotFoundError,
)
from ledger_api.services.accounts import (
    get_customer_account,
    list_customer_accounts,
    open_customer_account,
)
from ledger_api.services.users import register_user
from tests.conftest import SessionFactory
from tests.factories import create_user, get_settlement_account

# --- opening accounts --------------------------------------------------------------------------


@pytest.mark.parametrize("currency", list(Currency))
async def test_open_account_creates_an_empty_customer_account_in_the_currency_ledger(
    new_session: SessionFactory, db_session: AsyncSession, currency: Currency
) -> None:
    owner = await create_user(db_session)

    account = await open_customer_account(new_session(), owner_id=owner.id, currency=currency)

    # Values returned by the INSERT itself, without a refresh.
    assert account.balance_minor == 0
    assert account.kind is AccountKind.CUSTOMER
    assert account.user_id == owner.id
    assert account.ledger.currency is currency
    assert account.id.version == 7


async def test_user_may_open_one_account_per_currency(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    owner = await create_user(db_session)

    gbp = await open_customer_account(new_session(), owner_id=owner.id, currency=Currency.GBP)
    eur = await open_customer_account(new_session(), owner_id=owner.id, currency=Currency.EUR)

    assert gbp.ledger_id != eur.ledger_id


async def test_second_account_in_the_same_currency_is_rejected(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    owner = await create_user(db_session)
    await open_customer_account(new_session(), owner_id=owner.id, currency=Currency.GBP)

    with pytest.raises(AccountAlreadyExistsError):
        await open_customer_account(new_session(), owner_id=owner.id, currency=Currency.GBP)


async def test_account_for_an_unknown_user_is_rejected(new_session: SessionFactory) -> None:
    with pytest.raises(UserNotFoundError):
        await open_customer_account(new_session(), owner_id=uuid.uuid7(), currency=Currency.GBP)


# --- owner-scoped lookup -----------------------------------------------------------------------


async def test_owner_can_get_their_account(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    owner = await create_user(db_session)
    opened = await open_customer_account(new_session(), owner_id=owner.id, currency=Currency.EUR)

    async with new_session() as session:
        found = await get_customer_account(session, owner_id=owner.id, account_id=opened.id)

    # The ledger was eagerly loaded, so it is usable after the session is closed.
    assert found.id == opened.id
    assert found.ledger.currency is Currency.EUR


async def test_another_users_account_is_not_found(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    owner = await create_user(db_session)
    intruder = await create_user(db_session)
    account = await open_customer_account(new_session(), owner_id=owner.id, currency=Currency.GBP)

    with pytest.raises(AccountNotFoundError):
        await get_customer_account(new_session(), owner_id=intruder.id, account_id=account.id)


async def test_system_account_is_not_found_through_any_owner(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    user = await create_user(db_session)
    settlement = await get_settlement_account(db_session, Currency.GBP)

    with pytest.raises(AccountNotFoundError):
        await get_customer_account(new_session(), owner_id=user.id, account_id=settlement.id)


async def test_nonexistent_account_is_not_found(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    user = await create_user(db_session)

    with pytest.raises(AccountNotFoundError):
        await get_customer_account(new_session(), owner_id=user.id, account_id=uuid.uuid7())


async def test_list_returns_only_the_owners_accounts_in_creation_order(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    owner = await create_user(db_session)
    other = await create_user(db_session)
    eur = await open_customer_account(new_session(), owner_id=owner.id, currency=Currency.EUR)
    gbp = await open_customer_account(new_session(), owner_id=owner.id, currency=Currency.GBP)
    await open_customer_account(new_session(), owner_id=other.id, currency=Currency.GBP)

    accounts = await list_customer_accounts(new_session(), owner_id=owner.id)

    assert [account.id for account in accounts] == [eur.id, gbp.id]
    assert all(account.kind is AccountKind.CUSTOMER for account in accounts)


async def test_list_for_a_user_without_accounts_is_empty(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    user = await create_user(db_session)

    assert await list_customer_accounts(new_session(), owner_id=user.id) == []


async def test_list_for_an_unknown_user_raises(new_session: SessionFactory) -> None:
    with pytest.raises(UserNotFoundError):
        await list_customer_accounts(new_session(), owner_id=uuid.uuid7())


async def test_ledger_relationship_never_lazy_loads(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    owner = await create_user(db_session)
    opened = await open_customer_account(new_session(), owner_id=owner.id, currency=Currency.GBP)

    async with new_session() as session:
        plain = await session.get(Account, opened.id)  # no eager load requested
        assert plain is not None
        with pytest.raises(InvalidRequestError, match="lazy='raise'"):
            _ = plain.ledger


# --- concurrency -------------------------------------------------------------------------------


@pytest.mark.concurrency
async def test_concurrent_openings_in_one_currency_create_exactly_one_account(
    engine: AsyncEngine,
) -> None:
    # Real sessions and COMMITs: the unique constraint decides the race, not a prior SELECT.
    # (Leaves one user and one account behind in the disposable test database.)
    sessionmaker = create_sessionmaker(engine)
    async with sessionmaker() as session:
        owner = await register_user(session, email=f"race-{uuid.uuid7().hex}@example.com")
    attempts = 5

    async def attempt() -> Account:
        async with sessionmaker() as session:
            return await open_customer_account(session, owner_id=owner.id, currency=Currency.GBP)

    results = await asyncio.gather(*(attempt() for _ in range(attempts)), return_exceptions=True)

    opened = [result for result in results if isinstance(result, Account)]
    rejected = [result for result in results if isinstance(result, AccountAlreadyExistsError)]
    assert len(opened) == 1
    assert len(rejected) == attempts - 1
    async with sessionmaker() as session:
        count = await session.scalar(select(func.count()).where(Account.user_id == owner.id))
    assert count == 1
