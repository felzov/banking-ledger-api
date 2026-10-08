"""Deposits, withdrawals and transfers through the posting services (rolled-back connection).

Accounts are funded only through deposits, so every test can end with a global
reconciliation: balances equal their entries and every ledger sums to zero.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import Account, LedgerEntry, Transaction, User
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import (
    AccountNotFoundError,
    CurrencyMismatchError,
    DestinationAccountNotFoundError,
    InsufficientFundsError,
    InvalidAmountError,
    SameAccountTransferError,
)
from ledger_api.domain.transaction import TransactionKind
from ledger_api.services.posting import PostingResult, deposit, transfer, withdraw
from tests.conftest import SessionFactory
from tests.database import assert_reconciled
from tests.factories import create_customer_account, create_user, get_settlement_account


async def _account(
    db_session: AsyncSession, currency: Currency = Currency.GBP, owner: User | None = None
) -> Account:
    return await create_customer_account(db_session, currency, user=owner)


async def _owner(account: Account) -> uuid.UUID:
    assert account.user_id is not None
    return account.user_id


async def _fund(new_session: SessionFactory, account: Account, amount: int) -> PostingResult:
    return await deposit(
        new_session(), owner_id=await _owner(account), account_id=account.id, amount_minor=amount
    )


async def _balance(db_session: AsyncSession, account_id: uuid.UUID) -> int | None:
    return await db_session.scalar(select(Account.balance_minor).where(Account.id == account_id))


async def _transaction_count(db_session: AsyncSession) -> int | None:
    return await db_session.scalar(select(func.count()).select_from(Transaction))


# --- deposits ----------------------------------------------------------------------------------


@pytest.mark.parametrize("currency", list(Currency))
async def test_deposit_credits_the_account_from_the_settlement_account(
    new_session: SessionFactory, db_session: AsyncSession, currency: Currency
) -> None:
    account = await _account(db_session, currency)
    settlement = await get_settlement_account(db_session, currency)
    settlement_before = await _balance(db_session, settlement.id)
    assert settlement_before is not None

    result = await _fund(new_session, account, 10_000)

    assert result.kind is TransactionKind.DEPOSIT
    assert result.balances == {account.id: 10_000}
    assert await _balance(db_session, settlement.id) == settlement_before - 10_000
    stored = await db_session.get(Transaction, result.transaction_id)
    assert stored is not None
    assert (stored.kind, stored.entry_count, stored.ledger_id) == (
        TransactionKind.DEPOSIT,
        2,
        account.ledger_id,
    )
    entries = await db_session.execute(
        select(LedgerEntry.account_id, LedgerEntry.amount_minor).where(
            LedgerEntry.transaction_id == result.transaction_id
        )
    )
    assert {tuple(row) for row in entries} == {(settlement.id, -10_000), (account.id, 10_000)}
    await assert_reconciled(db_session)


async def test_deposit_into_someone_elses_account_is_not_found(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    account = await _account(db_session)
    stranger = await create_user(db_session)

    with pytest.raises(AccountNotFoundError):
        await deposit(new_session(), owner_id=stranger.id, account_id=account.id, amount_minor=1)


async def test_deposit_into_an_unknown_account_is_not_found(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    user = await create_user(db_session)

    with pytest.raises(AccountNotFoundError):
        await deposit(new_session(), owner_id=user.id, account_id=uuid.uuid7(), amount_minor=1)


async def test_deposit_into_a_settlement_account_is_not_found(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    user = await create_user(db_session)
    settlement = await get_settlement_account(db_session, Currency.GBP)

    with pytest.raises(AccountNotFoundError):
        await deposit(new_session(), owner_id=user.id, account_id=settlement.id, amount_minor=1)


# --- withdrawals -------------------------------------------------------------------------------


async def test_withdrawal_debits_the_account_to_the_settlement_account(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    account = await _account(db_session)
    await _fund(new_session, account, 10_000)

    result = await withdraw(
        new_session(), owner_id=await _owner(account), account_id=account.id, amount_minor=2_500
    )

    assert result.kind is TransactionKind.WITHDRAWAL
    assert result.balances == {account.id: 7_500}
    await assert_reconciled(db_session)


async def test_withdrawing_the_exact_balance_leaves_zero(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    account = await _account(db_session)
    await _fund(new_session, account, 3_000)

    result = await withdraw(
        new_session(), owner_id=await _owner(account), account_id=account.id, amount_minor=3_000
    )

    assert result.balances == {account.id: 0}


async def test_withdrawing_one_more_than_the_balance_is_rejected_and_changes_nothing(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    account = await _account(db_session)
    await _fund(new_session, account, 3_000)
    transactions_before = await _transaction_count(db_session)

    with pytest.raises(InsufficientFundsError):
        await withdraw(
            new_session(), owner_id=await _owner(account), account_id=account.id, amount_minor=3_001
        )

    assert await _balance(db_session, account.id) == 3_000
    assert await _transaction_count(db_session) == transactions_before
    await assert_reconciled(db_session)


async def test_withdrawal_from_someone_elses_account_is_not_found(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    account = await _account(db_session)
    await _fund(new_session, account, 1_000)
    stranger = await create_user(db_session)

    with pytest.raises(AccountNotFoundError):
        await withdraw(new_session(), owner_id=stranger.id, account_id=account.id, amount_minor=1)
    assert await _balance(db_session, account.id) == 1_000


# --- transfers ---------------------------------------------------------------------------------


async def test_transfer_moves_money_between_customers(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    source = await _account(db_session)
    destination = await _account(db_session)
    await _fund(new_session, source, 5_000)

    result = await transfer(
        new_session(),
        owner_id=await _owner(source),
        source_account_id=source.id,
        destination_account_id=destination.id,
        amount_minor=3_000,
    )

    assert result.kind is TransactionKind.TRANSFER
    # The caller learns its own new balance, never the destination's.
    assert result.balances == {source.id: 2_000}
    assert await _balance(db_session, destination.id) == 3_000
    await assert_reconciled(db_session)


async def test_transfer_between_a_users_own_accounts(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    owner = await create_user(db_session)
    # One account per currency per user, so "own accounts" means another user is not needed:
    # a transfer to oneself in another currency is a currency mismatch (tested below).
    source = await _account(db_session, owner=owner)
    other = await _account(db_session)
    await _fund(new_session, source, 100)

    result = await transfer(
        new_session(),
        owner_id=owner.id,
        source_account_id=source.id,
        destination_account_id=other.id,
        amount_minor=100,
    )

    assert result.balances == {source.id: 0}


async def test_transfer_with_insufficient_funds_is_rejected_and_changes_nothing(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    source = await _account(db_session)
    destination = await _account(db_session)
    await _fund(new_session, source, 100)

    with pytest.raises(InsufficientFundsError):
        await transfer(
            new_session(),
            owner_id=await _owner(source),
            source_account_id=source.id,
            destination_account_id=destination.id,
            amount_minor=101,
        )

    assert (await _balance(db_session, source.id), await _balance(db_session, destination.id)) == (
        100,
        0,
    )
    await assert_reconciled(db_session)


async def test_transfer_to_another_currency_is_rejected(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    owner = await create_user(db_session)
    gbp = await _account(db_session, Currency.GBP, owner)
    eur = await _account(db_session, Currency.EUR, owner)
    await _fund(new_session, gbp, 1_000)

    with pytest.raises(CurrencyMismatchError):
        await transfer(
            new_session(),
            owner_id=owner.id,
            source_account_id=gbp.id,
            destination_account_id=eur.id,
            amount_minor=100,
        )
    assert await _balance(db_session, gbp.id) == 1_000


async def test_transfer_to_the_same_account_is_rejected_before_any_database_work(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    account = await _account(db_session)
    session = new_session()

    with pytest.raises(SameAccountTransferError):
        await transfer(
            session,
            owner_id=await _owner(account),
            source_account_id=account.id,
            destination_account_id=account.id,
            amount_minor=100,
        )
    assert not session.in_transaction()


@pytest.mark.parametrize("destination_kind", ["unknown", "settlement"])
async def test_transfer_to_an_invalid_destination_is_rejected(
    new_session: SessionFactory, db_session: AsyncSession, destination_kind: str
) -> None:
    source = await _account(db_session)
    await _fund(new_session, source, 1_000)
    destination_id = (
        uuid.uuid7()
        if destination_kind == "unknown"
        else (await get_settlement_account(db_session, Currency.GBP)).id
    )

    with pytest.raises(DestinationAccountNotFoundError):
        await transfer(
            new_session(),
            owner_id=await _owner(source),
            source_account_id=source.id,
            destination_account_id=destination_id,
            amount_minor=100,
        )
    assert await _balance(db_session, source.id) == 1_000


async def test_transfer_from_someone_elses_account_is_not_found(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    victim = await _account(db_session)
    thief_account = await _account(db_session)
    await _fund(new_session, victim, 1_000)

    with pytest.raises(AccountNotFoundError):
        await transfer(
            new_session(),
            owner_id=await _owner(thief_account),
            source_account_id=victim.id,
            destination_account_id=thief_account.id,
            amount_minor=1_000,
        )
    assert await _balance(db_session, victim.id) == 1_000


# --- amounts -----------------------------------------------------------------------------------


@pytest.mark.parametrize("amount", [0, -100, 100_000_001])
async def test_invalid_amounts_are_rejected_before_any_database_work(
    new_session: SessionFactory, db_session: AsyncSession, amount: int
) -> None:
    account = await _account(db_session)
    session = new_session()

    with pytest.raises(InvalidAmountError):
        await deposit(
            session, owner_id=await _owner(account), account_id=account.id, amount_minor=amount
        )
    assert not session.in_transaction()


# --- the projection seen by the rest of the system ---------------------------------------------


async def test_settlement_mirrors_customer_money_after_mixed_postings(
    new_session: SessionFactory, db_session: AsyncSession
) -> None:
    first, second = await _account(db_session), await _account(db_session)
    await _fund(new_session, first, 9_000)
    await transfer(
        new_session(),
        owner_id=await _owner(first),
        source_account_id=first.id,
        destination_account_id=second.id,
        amount_minor=4_000,
    )
    await withdraw(
        new_session(), owner_id=await _owner(second), account_id=second.id, amount_minor=1_500
    )

    assert (await _balance(db_session, first.id), await _balance(db_session, second.id)) == (
        5_000,
        2_500,
    )
    await assert_reconciled(db_session)  # includes: settlement = -(sum of customer balances)


async def test_account_api_shows_the_posted_balance(
    api_client: AsyncClient, new_session: SessionFactory, db_session: AsyncSession
) -> None:
    account = await _account(db_session)
    owner_id = await _owner(account)
    await _fund(new_session, account, 4_200)

    response = await api_client.get(f"/users/{owner_id}/accounts/{account.id}")

    assert response.status_code == 200
    assert response.json()["balance_minor"] == 4_200
