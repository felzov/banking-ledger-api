"""Idempotent postings (ADR 0006), with real COMMITs.

- A key is consumed only by a committed posting: rejected and failed requests leave no key, and
  their retry with the same key runs normally.
- A retry with the same key and the same request replays the original result, rebuilt from the
  ledger (historical balance_after, never the current balance), and writes nothing.
- The same key with a different request is refused (idempotency_key_mismatch), and audited.
- Keys are scoped per user.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger_api.data import posting as data_posting
from ledger_api.data.audit import insert_audit_event
from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.models import Account, AuditEvent, IdempotencyKey, LedgerEntry, Transaction
from ledger_api.domain.audit import AuditOutcome, AuditRecord
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import (
    AccountNotFoundError,
    IdempotencyKeyMismatchError,
    InsufficientFundsError,
)
from ledger_api.domain.idempotency import request_fingerprint
from ledger_api.domain.transaction import TransactionKind
from ledger_api.services import audit as audit_service
from ledger_api.services import posting as posting_service
from ledger_api.services.accounts import open_customer_account
from ledger_api.services.posting import PostingResult, deposit, transfer, withdraw
from tests.database import assert_reconciled, audit_events_about
from tests.factories import FundedAccount, commit_funded_account

AUDIT_LOGGER = "ledger_api.services.audit"

type Maker = async_sessionmaker[AsyncSession]


class InjectedFailureError(Exception):
    pass


class _DriverError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"SQLSTATE {code}")
        self.sqlstate = code


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> Maker:
    return create_sessionmaker(engine)


@pytest.fixture
async def funded(sessionmaker: Maker) -> FundedAccount:
    return await commit_funded_account(sessionmaker, 5_000)


@pytest.fixture
async def empty(sessionmaker: Maker) -> FundedAccount:
    return await commit_funded_account(sessionmaker, 0)


def _key() -> str:
    return f"key-{uuid.uuid7().hex}"


async def _deposit(sm: Maker, account: FundedAccount, amount: int, key: str) -> PostingResult:
    async with sm() as session:
        return await deposit(
            session,
            owner_id=account.owner_id,
            account_id=account.account_id,
            amount_minor=amount,
            idempotency_key=key,
        )


async def _withdraw(sm: Maker, account: FundedAccount, amount: int, key: str) -> PostingResult:
    async with sm() as session:
        return await withdraw(
            session,
            owner_id=account.owner_id,
            account_id=account.account_id,
            amount_minor=amount,
            idempotency_key=key,
        )


async def _transfer(
    sm: Maker, source: FundedAccount, destination: uuid.UUID, amount: int, key: str
) -> PostingResult:
    async with sm() as session:
        return await transfer(
            session,
            owner_id=source.owner_id,
            source_account_id=source.account_id,
            destination_account_id=destination,
            amount_minor=amount,
            idempotency_key=key,
        )


async def _key_row(sm: Maker, owner_id: uuid.UUID, key: str) -> IdempotencyKey | None:
    async with sm() as session:
        return await session.get(IdempotencyKey, (owner_id, key))


async def _entry_count(sm: Maker, account_id: uuid.UUID) -> int:
    async with sm() as session:
        statement = select(func.count()).where(LedgerEntry.account_id == account_id)
        return await session.scalar(statement) or 0


async def _balance(sm: Maker, account_id: uuid.UUID) -> int:
    async with sm() as session:
        balance = await session.scalar(
            select(Account.balance_minor).where(Account.id == account_id)
        )
    assert balance is not None
    return balance


async def _events(sm: Maker, account_id: uuid.UUID) -> list[AuditEvent]:
    async with sm() as session:
        return await audit_events_about(session, account_id)


async def _assert_reconciled(sm: Maker) -> None:
    async with sm() as session:
        await assert_reconciled(session)


# --- first request ------------------------------------------------------------------------------


async def test_a_keyed_posting_commits_its_key_with_it(
    sessionmaker: Maker, empty: FundedAccount
) -> None:
    key = _key()

    result = await _deposit(sessionmaker, empty, 1_000, key)

    assert not result.replayed
    assert result.balances == {empty.account_id: 1_000}
    row = await _key_row(sessionmaker, empty.owner_id, key)
    assert row is not None
    assert row.transaction_id == result.transaction_id
    assert row.request_fingerprint == request_fingerprint(
        operation=TransactionKind.DEPOSIT,
        user_id=empty.owner_id,
        account_id=empty.account_id,
        destination_account_id=None,
        amount_minor=1_000,
    )
    *_, event = await _events(sessionmaker, empty.account_id)
    assert event.outcome == AuditOutcome.SUCCEEDED
    assert event.transaction_id == result.transaction_id
    assert event.idempotency_key == key
    await _assert_reconciled(sessionmaker)


# --- duplicates ---------------------------------------------------------------------------------


async def test_a_retried_deposit_replays_without_writing(
    sessionmaker: Maker, empty: FundedAccount
) -> None:
    key = _key()
    original = await _deposit(sessionmaker, empty, 1_000, key)
    entries, events = (
        await _entry_count(sessionmaker, empty.account_id),
        await _events(sessionmaker, empty.account_id),
    )

    replayed = await _deposit(sessionmaker, empty, 1_000, key)

    assert replayed.replayed
    assert (replayed.transaction_id, replayed.kind, replayed.created_at) == (
        original.transaction_id,
        original.kind,
        original.created_at,
    )
    assert replayed.balances == original.balances
    assert await _entry_count(sessionmaker, empty.account_id) == entries
    assert await _balance(sessionmaker, empty.account_id) == 1_000
    # Replays are not audited (ADR 0007): nothing happened.
    assert [event.id for event in await _events(sessionmaker, empty.account_id)] == [
        event.id for event in events
    ]


async def test_a_retried_withdrawal_replays_without_writing(
    sessionmaker: Maker, funded: FundedAccount
) -> None:
    key = _key()
    original = await _withdraw(sessionmaker, funded, 1_200, key)

    replayed = await _withdraw(sessionmaker, funded, 1_200, key)

    assert replayed.replayed
    assert replayed.transaction_id == original.transaction_id
    assert replayed.balances == original.balances == {funded.account_id: 3_800}
    assert await _balance(sessionmaker, funded.account_id) == 3_800


async def test_a_retried_transfer_replays_only_the_callers_balance(
    sessionmaker: Maker, funded: FundedAccount, empty: FundedAccount
) -> None:
    key = _key()
    original = await _transfer(sessionmaker, funded, empty.account_id, 700, key)

    replayed = await _transfer(sessionmaker, funded, empty.account_id, 700, key)

    assert replayed.replayed
    assert replayed.transaction_id == original.transaction_id
    assert replayed.balances == original.balances == {funded.account_id: 4_300}
    assert await _balance(sessionmaker, empty.account_id) == 700


async def test_a_replay_reports_the_historical_balance_not_the_current_one(
    sessionmaker: Maker, empty: FundedAccount
) -> None:
    first_deposit, withdrawal = _key(), _key()
    await _deposit(sessionmaker, empty, 1_000, first_deposit)  # balance after: 1,000
    await _deposit(sessionmaker, empty, 500, _key())  # 1,500
    await _withdraw(sessionmaker, empty, 200, withdrawal)  # 1,300
    await _deposit(sessionmaker, empty, 700, _key())  # 2,000
    await _withdraw(sessionmaker, empty, 50, _key())  # 1,950
    assert await _balance(sessionmaker, empty.account_id) == 1_950

    replayed_deposit = await _deposit(sessionmaker, empty, 1_000, first_deposit)
    replayed_withdrawal = await _withdraw(sessionmaker, empty, 200, withdrawal)

    assert replayed_deposit.balances == {empty.account_id: 1_000}
    assert replayed_withdrawal.balances == {empty.account_id: 1_300}
    assert await _balance(sessionmaker, empty.account_id) == 1_950  # untouched by the replays


async def test_a_transfer_replay_is_historical_after_the_payee_spends_and_pays_back(
    sessionmaker: Maker, funded: FundedAccount, empty: FundedAccount
) -> None:
    key = _key()
    await _transfer(sessionmaker, funded, empty.account_id, 1_000, key)  # payer: 4,000
    await _transfer(sessionmaker, empty, funded.account_id, 600, _key())  # payer: 4,600

    replayed = await _transfer(sessionmaker, funded, empty.account_id, 1_000, key)

    assert replayed.balances == {funded.account_id: 4_000}
    assert await _balance(sessionmaker, funded.account_id) == 4_600


# --- conflicting payloads -----------------------------------------------------------------------


async def _assert_refused_without_writes(
    sessionmaker: Maker,
    owner_id: uuid.UUID,
    key: str,
    original: PostingResult,
    retry: Callable[[], Awaitable[PostingResult]],
    audited_account_id: uuid.UUID,
) -> None:
    before = await _entry_count(sessionmaker, audited_account_id)

    with pytest.raises(IdempotencyKeyMismatchError):
        await retry()

    assert await _entry_count(sessionmaker, audited_account_id) == before
    row = await _key_row(sessionmaker, owner_id, key)
    assert row is not None
    assert row.transaction_id == original.transaction_id  # the original stands, unchanged
    *_, event = await _events(sessionmaker, audited_account_id)
    assert (event.outcome, event.reason) == (AuditOutcome.REJECTED, "idempotency_key_mismatch")
    assert event.idempotency_key == key
    assert event.transaction_id is None
    await _assert_reconciled(sessionmaker)


async def test_same_key_with_a_different_amount_is_refused(
    sessionmaker: Maker, funded: FundedAccount
) -> None:
    key = _key()
    original = await _deposit(sessionmaker, funded, 1_000, key)

    await _assert_refused_without_writes(
        sessionmaker,
        funded.owner_id,
        key,
        original,
        lambda: _deposit(sessionmaker, funded, 1_001, key),
        funded.account_id,
    )
    assert await _balance(sessionmaker, funded.account_id) == 6_000


async def test_same_key_for_a_different_operation_is_refused(
    sessionmaker: Maker, funded: FundedAccount
) -> None:
    key = _key()
    original = await _deposit(sessionmaker, funded, 1_000, key)

    await _assert_refused_without_writes(
        sessionmaker,
        funded.owner_id,
        key,
        original,
        lambda: _withdraw(sessionmaker, funded, 1_000, key),
        funded.account_id,
    )
    assert await _balance(sessionmaker, funded.account_id) == 6_000


async def test_same_key_on_another_account_of_the_same_user_is_refused(
    sessionmaker: Maker, funded: FundedAccount
) -> None:
    key = _key()
    original = await _deposit(sessionmaker, funded, 1_000, key)
    async with sessionmaker() as session:
        euro = await open_customer_account(session, owner_id=funded.owner_id, currency=Currency.EUR)
    euro_account = FundedAccount(funded.owner_id, euro.id)

    await _assert_refused_without_writes(
        sessionmaker,
        funded.owner_id,
        key,
        original,
        lambda: _deposit(sessionmaker, euro_account, 1_000, key),
        euro.id,
    )
    assert await _balance(sessionmaker, euro.id) == 0


async def test_same_key_with_a_different_destination_is_refused(
    sessionmaker: Maker, funded: FundedAccount, empty: FundedAccount
) -> None:
    other = await commit_funded_account(sessionmaker, 0)
    key = _key()
    original = await _transfer(sessionmaker, funded, empty.account_id, 300, key)

    await _assert_refused_without_writes(
        sessionmaker,
        funded.owner_id,
        key,
        original,
        lambda: _transfer(sessionmaker, funded, other.account_id, 300, key),
        funded.account_id,
    )
    assert await _balance(sessionmaker, other.account_id) == 0


async def test_keys_are_scoped_per_user(
    sessionmaker: Maker, funded: FundedAccount, empty: FundedAccount
) -> None:
    key = _key()

    mine = await _deposit(sessionmaker, funded, 100, key)
    theirs = await _deposit(sessionmaker, empty, 100, key)

    assert not mine.replayed
    assert not theirs.replayed
    assert mine.transaction_id != theirs.transaction_id
    assert await _balance(sessionmaker, empty.account_id) == 100


# --- rejected, then retried ---------------------------------------------------------------------


async def test_a_rejected_request_leaves_no_key_and_its_retry_runs(
    sessionmaker: Maker, empty: FundedAccount
) -> None:
    key = _key()

    with pytest.raises(InsufficientFundsError):
        await _withdraw(sessionmaker, empty, 500, key)

    assert await _key_row(sessionmaker, empty.owner_id, key) is None
    *_, rejected = await _events(sessionmaker, empty.account_id)
    assert (rejected.outcome, rejected.reason) == (AuditOutcome.REJECTED, "insufficient_funds")
    assert rejected.idempotency_key == key

    await _deposit(sessionmaker, empty, 1_000, _key())
    retried = await _withdraw(sessionmaker, empty, 500, key)  # same key, same request: it runs

    assert not retried.replayed
    assert retried.balances == {empty.account_id: 500}
    row = await _key_row(sessionmaker, empty.owner_id, key)
    assert row is not None
    assert row.transaction_id == retried.transaction_id

    again = await _withdraw(sessionmaker, empty, 500, key)  # now it is consumed: replay
    assert again.replayed
    assert again.transaction_id == retried.transaction_id
    assert await _balance(sessionmaker, empty.account_id) == 500
    await _assert_reconciled(sessionmaker)


async def test_a_request_for_an_unknown_user_is_account_not_found_and_leaves_no_key(
    sessionmaker: Maker, funded: FundedAccount
) -> None:
    stranger = FundedAccount(uuid.uuid7(), funded.account_id)
    key = _key()

    with pytest.raises(AccountNotFoundError) as excinfo:
        await _deposit(sessionmaker, stranger, 100, key)

    # The claim is what failed (the key's user does not exist), translated by exact name.
    assert isinstance(excinfo.value.__cause__, IntegrityError)
    assert await _key_row(sessionmaker, stranger.owner_id, key) is None
    *_, event = await _events(sessionmaker, funded.account_id)
    assert (event.outcome, event.reason) == (AuditOutcome.REJECTED, "account_not_found")
    assert event.actor_user_id is None
    assert event.details["requested_user_id"] == str(stranger.owner_id)


@pytest.mark.parametrize("key", ["", "with space", "K" * 256])
async def test_a_malformed_key_is_a_programming_error_and_is_not_audited(
    sessionmaker: Maker, funded: FundedAccount, key: str
) -> None:
    events = await _events(sessionmaker, funded.account_id)

    with pytest.raises(ValueError, match="idempotency key"):
        await _deposit(sessionmaker, funded, 100, key)

    assert len(await _events(sessionmaker, funded.account_id)) == len(events)


# --- failures: nothing committed, so no key, and the retry runs once ------------------------------


async def _assert_failed_without_key_then_retry_posts_once(
    sessionmaker: Maker, account: FundedAccount, key: str, amount: int
) -> None:
    assert await _key_row(sessionmaker, account.owner_id, key) is None
    assert await _balance(sessionmaker, account.account_id) == 5_000
    *_, failed = await _events(sessionmaker, account.account_id)
    assert failed.outcome == AuditOutcome.FAILED
    assert failed.idempotency_key == key

    retried = await _withdraw(sessionmaker, account, amount, key)

    assert not retried.replayed
    assert await _balance(sessionmaker, account.account_id) == 5_000 - amount
    row = await _key_row(sessionmaker, account.owner_id, key)
    assert row is not None
    assert row.transaction_id == retried.transaction_id
    await _assert_reconciled(sessionmaker)


async def test_a_failure_after_the_entries_leaves_no_key(
    sessionmaker: Maker, funded: FundedAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_balance_update(*args: Any, **kwargs: Any) -> dict[uuid.UUID, int]:
        raise InjectedFailureError

    key = _key()
    with monkeypatch.context() as patch:
        patch.setattr(posting_service, "apply_balance_deltas", fail_balance_update)
        with pytest.raises(InjectedFailureError):
            await _withdraw(sessionmaker, funded, 1_000, key)

    await _assert_failed_without_key_then_retry_posts_once(sessionmaker, funded, key, 1_000)


async def test_a_deferred_failure_at_commit_leaves_no_key(
    sessionmaker: Maker, funded: FundedAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    def miscounted(**values: Any) -> Transaction:
        return Transaction(**{**values, "entry_count": values["entry_count"] + 1})

    key = _key()
    with monkeypatch.context() as patch:
        patch.setattr(data_posting, "Transaction", miscounted)
        with pytest.raises(IntegrityError):
            await _withdraw(sessionmaker, funded, 1_000, key)

    await _assert_failed_without_key_then_retry_posts_once(sessionmaker, funded, key, 1_000)


async def test_a_failed_success_audit_leaves_no_key(
    sessionmaker: Maker, funded: FundedAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_success_write(session: AsyncSession, record: AuditRecord) -> None:
        if record.outcome == AuditOutcome.SUCCEEDED:
            raise InjectedFailureError
        await insert_audit_event(session, record)

    key = _key()
    with monkeypatch.context() as patch:
        patch.setattr(audit_service, "insert_audit_event", failing_success_write)
        with pytest.raises(InjectedFailureError):
            await _withdraw(sessionmaker, funded, 1_000, key)

    await _assert_failed_without_key_then_retry_posts_once(sessionmaker, funded, key, 1_000)


async def test_a_deadlock_retry_claims_the_key_exactly_once(
    sessionmaker: Maker, funded: FundedAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first attempt claims the key and writes everything, then deadlocks: its claim rolls
    # back with it, and the retry claims the key again with its own transaction id.
    real_record = audit_service.record_in_transaction
    calls = 0

    async def record_then_deadlock_once(session: AsyncSession, record: AuditRecord) -> None:
        nonlocal calls
        calls += 1
        await real_record(session, record)
        if calls == 1:
            raise DBAPIError("COMMIT", None, _DriverError("40P01"))

    monkeypatch.setattr(posting_service, "record_in_transaction", record_then_deadlock_once)
    key = _key()

    result = await _withdraw(sessionmaker, funded, 1_000, key)

    assert calls == 2
    row = await _key_row(sessionmaker, funded.owner_id, key)
    assert row is not None
    assert row.transaction_id == result.transaction_id
    assert await _balance(sessionmaker, funded.account_id) == 4_000
    async with sessionmaker() as session:
        keyed = await session.scalar(
            select(func.count()).where(
                IdempotencyKey.user_id == funded.owner_id, IdempotencyKey.key == key
            )
        )
    assert keyed == 1


# --- audit failures around idempotency ----------------------------------------------------------


async def test_a_failed_mismatch_audit_still_refuses_and_logs(
    sessionmaker: Maker,
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = _key()
    await _deposit(sessionmaker, funded, 1_000, key)

    async def failing_rejection_write(session: AsyncSession, record: AuditRecord) -> None:
        if record.outcome == AuditOutcome.REJECTED:
            raise InjectedFailureError("audit down")
        await insert_audit_event(session, record)

    monkeypatch.setattr(audit_service, "insert_audit_event", failing_rejection_write)

    with (
        caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER),
        pytest.raises(IdempotencyKeyMismatchError),
    ):
        await _deposit(sessionmaker, funded, 2_000, key)

    [log] = [record for record in caplog.records if record.name == AUDIT_LOGGER]
    assert "reason=idempotency_key_mismatch" in log.getMessage()
    assert f"idempotency_key={key}" in log.getMessage()
    assert await _balance(sessionmaker, funded.account_id) == 6_000


# --- session hygiene ----------------------------------------------------------------------------


async def test_replay_and_mismatch_leave_the_session_outside_a_transaction(
    sessionmaker: Maker, funded: FundedAccount
) -> None:
    key = _key()
    await _deposit(sessionmaker, funded, 100, key)

    async with sessionmaker() as session:
        replayed = await deposit(
            session,
            owner_id=funded.owner_id,
            account_id=funded.account_id,
            amount_minor=100,
            idempotency_key=key,
        )
        assert replayed.replayed
        assert not session.in_transaction()

        with pytest.raises(IdempotencyKeyMismatchError):
            await deposit(
                session,
                owner_id=funded.owner_id,
                account_id=funded.account_id,
                amount_minor=101,
                idempotency_key=key,
            )
        assert not session.in_transaction()

        # The same session can still post: nothing was left half-open.
        posted = await deposit(
            session,
            owner_id=funded.owner_id,
            account_id=funded.account_id,
            amount_minor=1,
            idempotency_key=_key(),
        )
        assert not posted.replayed
