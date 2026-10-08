"""Model-based property test: random sequences of postings against a pure-Python model.

Hypothesis generates sequences of deposits, withdrawals and transfers (including ones that
must be rejected). Each runs through the real services and PostgreSQL; after every step the
database balances must equal the model's, every rejection must be the one the model
predicts, and at the end the whole ledger must reconcile.
"""

import uuid
from dataclasses import dataclass

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from ledger_api.data.models import Account
from ledger_api.domain.errors import InsufficientFundsError, SameAccountTransferError
from ledger_api.services.posting import deposit, transfer, withdraw
from tests.conftest import SessionFactory
from tests.database import assert_reconciled
from tests.factories import create_customer_account

ACCOUNTS = 3
amounts = st.integers(min_value=1, max_value=5_000)
accounts = st.integers(min_value=0, max_value=ACCOUNTS - 1)


@dataclass(frozen=True)
class Deposit:
    account: int
    amount: int


@dataclass(frozen=True)
class Withdraw:
    account: int
    amount: int


@dataclass(frozen=True)
class Transfer:
    source: int
    destination: int
    amount: int


operations = st.lists(
    st.one_of(
        st.builds(Deposit, accounts, amounts),
        st.builds(Withdraw, accounts, amounts),
        st.builds(Transfer, accounts, accounts, amounts),  # source == destination included
    ),
    max_size=25,
)


def _expected(model: list[int], operation: Deposit | Withdraw | Transfer) -> type[Exception] | None:
    """Apply `operation` to the model; return the error the service must raise, if any."""
    match operation:
        case Deposit(account, amount):
            model[account] += amount
        case Withdraw(account, amount):
            if model[account] < amount:
                return InsufficientFundsError
            model[account] -= amount
        case Transfer(source, destination, amount):
            if source == destination:
                return SameAccountTransferError
            if model[source] < amount:
                return InsufficientFundsError
            model[source] -= amount
            model[destination] += amount
    return None


async def _apply(
    session: AsyncSession, ids: list[tuple[uuid.UUID, uuid.UUID]], operation: object
) -> None:
    match operation:
        case Deposit(account, amount):
            owner, account_id = ids[account]
            await deposit(session, owner_id=owner, account_id=account_id, amount_minor=amount)
        case Withdraw(account, amount):
            owner, account_id = ids[account]
            await withdraw(session, owner_id=owner, account_id=account_id, amount_minor=amount)
        case Transfer(source, destination, amount):
            owner, source_id = ids[source]
            await transfer(
                session,
                owner_id=owner,
                source_account_id=source_id,
                destination_account_id=ids[destination][1],
                amount_minor=amount,
            )


def _owner(account: Account) -> uuid.UUID:
    assert account.user_id is not None  # customer accounts always have an owner
    return account.user_id


# function_scoped_fixture: the fixtures provide the rolled-back connection; each example is
# isolated by its own SAVEPOINT (rolled back below), so sharing them across examples is safe.
@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(sequence=operations)
async def test_postings_match_a_pure_model_and_always_reconcile(
    db_connection: AsyncConnection,
    new_session: SessionFactory,
    sequence: list[Deposit | Withdraw | Transfer],
) -> None:
    example = await db_connection.begin_nested()
    try:
        # Committed, i.e. released into this example's SAVEPOINT: closing an uncommitted
        # session would roll the accounts back.
        async with new_session() as setup, setup.begin():
            created = [await create_customer_account(setup) for _ in range(ACCOUNTS)]
        ids = [(_owner(account), account.id) for account in created]
        model = [0] * ACCOUNTS

        for operation in sequence:
            expected_error = _expected(model, operation)
            if expected_error is None:
                await _apply(new_session(), ids, operation)
            else:
                with pytest.raises(expected_error):
                    await _apply(new_session(), ids, operation)

            async with new_session() as check:
                rows = await check.execute(
                    select(Account.id, Account.balance_minor).where(
                        Account.id.in_([account_id for _, account_id in ids])
                    )
                )
                balances = {account_id: balance for account_id, balance in rows}
            assert [balances[account_id] for _, account_id in ids] == model

        async with new_session() as check:
            await assert_reconciled(check)
    finally:
        await example.rollback()
