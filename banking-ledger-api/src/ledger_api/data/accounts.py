"""Account queries. Customer accounts are only ever looked up through their owner.

There is intentionally no unscoped "get account by id" here: every API-facing lookup must
name the owner, so reading another user's account cannot be written by accident (ADR 0009).
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ledger_api.data.models import Account, Ledger
from ledger_api.domain.currency import Currency


async def get_ledger_by_currency(session: AsyncSession, currency: Currency) -> Ledger:
    # Exactly one ledger per supported currency is seeded by migration 0001; a missing one is
    # a deployment bug, so .one() fails loudly rather than returning None.
    return (await session.scalars(select(Ledger).where(Ledger.currency == currency))).one()


def _owned_accounts(owner_id: uuid.UUID) -> Select[Account]:
    # user_id is non-NULL only for customer accounts (ck_accounts_owner_matches_kind), so an
    # owner-scoped query can never return a system (settlement) account.
    return select(Account).where(Account.user_id == owner_id).options(joinedload(Account.ledger))


async def get_owned_account(
    session: AsyncSession, *, owner_id: uuid.UUID, account_id: uuid.UUID
) -> Account | None:
    """The account if it exists AND belongs to owner_id; otherwise None, indistinguishably."""
    statement = _owned_accounts(owner_id).where(Account.id == account_id)
    return (await session.scalars(statement)).one_or_none()


async def list_owned_accounts(session: AsyncSession, *, owner_id: uuid.UUID) -> Sequence[Account]:
    statement = _owned_accounts(owner_id).order_by(Account.created_at, Account.id)
    return (await session.scalars(statement)).all()
