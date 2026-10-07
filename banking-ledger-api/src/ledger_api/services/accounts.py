import uuid
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.accounts import get_ledger_by_currency, get_owned_account, list_owned_accounts
from ledger_api.data.errors import violated_constraint
from ledger_api.data.models import Account
from ledger_api.domain.account import AccountKind
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import (
    AccountAlreadyExistsError,
    AccountNotFoundError,
    UserNotFoundError,
)
from ledger_api.services.users import get_user

ONE_ACCOUNT_PER_CURRENCY = "uq_accounts_user_id_ledger_id"
OWNER_MUST_EXIST = "fk_accounts_user_id_users"


async def open_customer_account(
    session: AsyncSession, *, owner_id: uuid.UUID, currency: Currency
) -> Account:
    """Open a customer account for owner_id in the ledger of `currency`.

    The kind is always customer and the balance always starts at 0 (server default); neither
    is ever taken from the caller. The database decides both failure cases, with no prior
    SELECT: a duplicate (user, currency) violates the unique constraint, and an unknown owner
    violates the foreign key.
    """
    try:
        async with session.begin():
            ledger = await get_ledger_by_currency(session, currency)
            account = Account(user_id=owner_id, kind=AccountKind.CUSTOMER, ledger=ledger)
            session.add(account)
            await session.flush()  # INSERT ... RETURNING id, balance_minor, created_at
    except IntegrityError as error:
        # Only the known rules are translated; any other violation is a bug and propagates.
        constraint = violated_constraint(error)
        if constraint == ONE_ACCOUNT_PER_CURRENCY:
            raise AccountAlreadyExistsError from error
        if constraint == OWNER_MUST_EXIST:
            raise UserNotFoundError from error
        raise
    return account


async def get_customer_account(
    session: AsyncSession, *, owner_id: uuid.UUID, account_id: uuid.UUID
) -> Account:
    account = await get_owned_account(session, owner_id=owner_id, account_id=account_id)
    if account is None:
        # Same error whether the account is missing, another user's, or a system account.
        raise AccountNotFoundError
    return account


async def list_customer_accounts(
    session: AsyncSession, *, owner_id: uuid.UUID
) -> Sequence[Account]:
    await get_user(session, user_id=owner_id)  # unknown user: 404, not an empty list
    return await list_owned_accounts(session, owner_id=owner_id)
