"""Every posting attempt is audited exactly once: succeeded events commit with the posting,
rejections are recorded after the rollback (ADR 0011). Real COMMITs throughout."""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.models import Account, AuditEvent, Transaction
from ledger_api.domain.audit import AuditAction, AuditOutcome
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import (
    AccountNotFoundError,
    CurrencyMismatchError,
    DestinationAccountNotFoundError,
    InsufficientFundsError,
    InvalidAmountError,
    SameAccountTransferError,
)
from ledger_api.services.posting import deposit, transfer, withdraw
from tests.database import assert_reconciled, audit_events_about
from tests.factories import FundedAccount, commit_funded_account

FUNDED = 5_000


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
async def funded(sessionmaker: async_sessionmaker[AsyncSession]) -> FundedAccount:
    return await commit_funded_account(sessionmaker, FUNDED)


async def _events(
    sessionmaker: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> list[AuditEvent]:
    async with sessionmaker() as session:
        return await audit_events_about(session, account_id)


async def _balance(sessionmaker: async_sessionmaker[AsyncSession], account_id: uuid.UUID) -> int:
    async with sessionmaker() as session:
        balance = await session.scalar(
            select(Account.balance_minor).where(Account.id == account_id)
        )
    assert balance is not None
    return balance


async def _transaction_count(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    async with sessionmaker() as session:
        return await session.scalar(select(func.count()).select_from(Transaction)) or 0


# --- succeeded ---------------------------------------------------------------------------------


async def test_funding_deposit_is_audited_as_succeeded(
    sessionmaker: async_sessionmaker[AsyncSession], funded: FundedAccount
) -> None:
    [event] = await _events(sessionmaker, funded.account_id)

    assert event.action == AuditAction.POSTING_DEPOSIT
    assert event.outcome == AuditOutcome.SUCCEEDED
    assert event.reason is None
    assert event.actor_user_id == funded.owner_id
    assert event.details == {"account_id": str(funded.account_id), "amount_minor": FUNDED}


async def test_withdrawal_success_event_references_the_committed_transaction(
    sessionmaker: async_sessionmaker[AsyncSession], funded: FundedAccount
) -> None:
    async with sessionmaker() as session:
        result = await withdraw(
            session, owner_id=funded.owner_id, account_id=funded.account_id, amount_minor=1_000
        )

    *_, event = await _events(sessionmaker, funded.account_id)
    assert (event.action, event.outcome) == (AuditAction.POSTING_WITHDRAWAL, AuditOutcome.SUCCEEDED)
    assert event.transaction_id == result.transaction_id
    async with sessionmaker() as session:
        # Committed in the posting's transaction: same transaction-start timestamp.
        posted = await session.get(Transaction, result.transaction_id)
        assert posted is not None
        assert event.occurred_at == posted.created_at


async def test_transfer_success_records_both_accounts_but_one_event(
    sessionmaker: async_sessionmaker[AsyncSession], funded: FundedAccount
) -> None:
    payee = await commit_funded_account(sessionmaker, 0)

    async with sessionmaker() as session:
        result = await transfer(
            session,
            owner_id=funded.owner_id,
            source_account_id=funded.account_id,
            destination_account_id=payee.account_id,
            amount_minor=700,
        )

    *_, event = await _events(sessionmaker, funded.account_id)
    assert event.action == AuditAction.POSTING_TRANSFER
    assert event.transaction_id == result.transaction_id
    assert event.details == {
        "account_id": str(funded.account_id),
        "destination_account_id": str(payee.account_id),
        "amount_minor": 700,
    }
    async with sessionmaker() as session:
        events_for_transaction = await session.scalar(
            select(func.count()).where(AuditEvent.transaction_id == result.transaction_id)
        )
    assert events_for_transaction == 1


# --- rejected ----------------------------------------------------------------------------------


async def test_insufficient_funds_changes_nothing_and_is_audited_once(
    sessionmaker: async_sessionmaker[AsyncSession], funded: FundedAccount
) -> None:
    transactions_before = await _transaction_count(sessionmaker)

    async with sessionmaker() as session:
        with pytest.raises(InsufficientFundsError):
            await withdraw(
                session,
                owner_id=funded.owner_id,
                account_id=funded.account_id,
                amount_minor=FUNDED + 1,
            )

    assert await _balance(sessionmaker, funded.account_id) == FUNDED
    assert await _transaction_count(sessionmaker) == transactions_before
    _funding, rejection = await _events(sessionmaker, funded.account_id)
    assert rejection.action == AuditAction.POSTING_WITHDRAWAL
    assert rejection.outcome == AuditOutcome.REJECTED
    assert rejection.reason == "insufficient_funds"
    assert rejection.transaction_id is None
    assert rejection.actor_user_id == funded.owner_id
    assert rejection.details == {"account_id": str(funded.account_id), "amount_minor": FUNDED + 1}
    async with sessionmaker() as session:
        await assert_reconciled(session)


async def test_posting_for_an_unknown_user_is_audited_without_an_actor(
    sessionmaker: async_sessionmaker[AsyncSession], funded: FundedAccount
) -> None:
    stranger = uuid.uuid7()

    async with sessionmaker() as session:
        with pytest.raises(AccountNotFoundError):
            await deposit(
                session, owner_id=stranger, account_id=funded.account_id, amount_minor=100
            )

    *_, event = await _events(sessionmaker, funded.account_id)
    assert (event.outcome, event.reason) == (AuditOutcome.REJECTED, "account_not_found")
    assert event.actor_user_id is None
    assert event.details["requested_user_id"] == str(stranger)


async def test_posting_on_someone_elses_account_names_the_real_caller(
    sessionmaker: async_sessionmaker[AsyncSession], funded: FundedAccount
) -> None:
    intruder = await commit_funded_account(sessionmaker, 0)

    async with sessionmaker() as session:
        with pytest.raises(AccountNotFoundError):
            await withdraw(
                session, owner_id=intruder.owner_id, account_id=funded.account_id, amount_minor=1
            )

    *_, event = await _events(sessionmaker, funded.account_id)
    assert (event.outcome, event.reason) == (AuditOutcome.REJECTED, "account_not_found")
    assert event.actor_user_id == intruder.owner_id
    assert await _balance(sessionmaker, funded.account_id) == FUNDED


@pytest.mark.parametrize(("amount", "recorded"), [(0, 0), (-5, -5), (10**12, 10**12), (1.5, None)])
async def test_invalid_amounts_are_audited_before_any_posting_work(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: FundedAccount,
    amount: int,
    recorded: int | None,
) -> None:
    async with sessionmaker() as session:
        with pytest.raises(InvalidAmountError):
            await deposit(
                session, owner_id=funded.owner_id, account_id=funded.account_id, amount_minor=amount
            )

    *_, event = await _events(sessionmaker, funded.account_id)
    assert (event.outcome, event.reason) == (AuditOutcome.REJECTED, "invalid_amount")
    # A non-integer amount is not recorded: it cannot be represented safely.
    assert event.details["amount_minor"] == recorded


@pytest.mark.parametrize(
    ("destination", "error", "reason"),
    [
        ("self", SameAccountTransferError, "same_account_transfer"),
        ("missing", DestinationAccountNotFoundError, "destination_account_not_found"),
        ("eur", CurrencyMismatchError, "currency_mismatch"),
    ],
)
async def test_rejected_transfers_are_audited_with_their_reason(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: FundedAccount,
    destination: str,
    error: type[Exception],
    reason: str,
) -> None:
    destination_id = {
        "self": funded.account_id,
        "missing": uuid.uuid7(),
        "eur": (await commit_funded_account(sessionmaker, 0, Currency.EUR)).account_id,
    }[destination]

    async with sessionmaker() as session:
        with pytest.raises(error):
            await transfer(
                session,
                owner_id=funded.owner_id,
                source_account_id=funded.account_id,
                destination_account_id=destination_id,
                amount_minor=100,
            )

    *_, event = await _events(sessionmaker, funded.account_id)
    assert (event.action, event.outcome, event.reason) == (
        AuditAction.POSTING_TRANSFER,
        AuditOutcome.REJECTED,
        reason,
    )
    assert event.details["destination_account_id"] == str(destination_id)
    assert await _balance(sessionmaker, funded.account_id) == FUNDED
