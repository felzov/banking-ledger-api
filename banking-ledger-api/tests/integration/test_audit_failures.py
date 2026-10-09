"""The audit protocol under failure (ADR 0011), with real COMMITs and injected faults.

- A succeeded event never survives a posting that did not commit, including one that fails at
  COMMIT on a deferred trigger.
- A failed posting rolls back completely, then a failed event is recorded.
- An audit write that fails never replaces the original error; it is logged at ERROR instead.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import URL, func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger_api.data import posting as data_posting
from ledger_api.data.audit import insert_audit_event
from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.models import Account, AuditEvent, Transaction
from ledger_api.domain.audit import AuditAction, AuditOutcome, AuditRecord
from ledger_api.domain.errors import InsufficientFundsError, TemporarilyUnavailableError
from ledger_api.services import audit as audit_service
from ledger_api.services import posting as posting_service
from ledger_api.services.posting import PostingResult, withdraw
from tests.database import assert_reconciled, audit_events_about
from tests.factories import FundedAccount, commit_funded_account

FUNDED = 5_000
WITHDRAWAL = 1_000
AUDIT_LOGGER = "ledger_api.services.audit"


class InjectedFailureError(Exception):
    pass


class _DriverError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"SQLSTATE {code}")
        self.sqlstate = code


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
async def funded(sessionmaker: async_sessionmaker[AsyncSession]) -> FundedAccount:
    return await commit_funded_account(sessionmaker, FUNDED)


async def _withdraw(
    sessionmaker: async_sessionmaker[AsyncSession], funded: FundedAccount, amount: int = WITHDRAWAL
) -> PostingResult:
    async with sessionmaker() as session:
        return await withdraw(
            session, owner_id=funded.owner_id, account_id=funded.account_id, amount_minor=amount
        )


async def _withdrawal_events(
    sessionmaker: async_sessionmaker[AsyncSession], funded: FundedAccount
) -> list[AuditEvent]:
    """Events after the funding deposit's."""
    async with sessionmaker() as session:
        funding, *rest = await audit_events_about(session, funded.account_id)
    assert funding.outcome == AuditOutcome.SUCCEEDED
    return rest


async def _assert_untouched(
    sessionmaker: async_sessionmaker[AsyncSession], funded: FundedAccount
) -> None:
    async with sessionmaker() as session:
        balance = await session.scalar(
            select(Account.balance_minor).where(Account.id == funded.account_id)
        )
        assert balance == FUNDED
        await assert_reconciled(session)


async def _transaction_exists(
    sessionmaker: async_sessionmaker[AsyncSession], transaction_id: object
) -> bool:
    assert isinstance(transaction_id, str)
    async with sessionmaker() as session:
        return await session.get(Transaction, uuid.UUID(transaction_id)) is not None


def _failing_audit_writes(
    *outcomes: AuditOutcome, error: Exception
) -> Callable[[AsyncSession, AuditRecord], Awaitable[None]]:
    """insert_audit_event that raises `error` for the given outcomes, and works otherwise."""

    async def insert(session: AsyncSession, record: AuditRecord) -> None:
        if record.outcome in outcomes:
            raise error
        await insert_audit_event(session, record)

    return insert


# --- failures of the posting -------------------------------------------------------------------


async def test_failure_after_the_entries_rolls_back_and_is_audited_as_failed(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected = InjectedFailureError()

    async def fail_balance_update(*args: Any, **kwargs: Any) -> dict[uuid.UUID, int]:
        raise injected  # header and entries are written; balances never are

    monkeypatch.setattr(posting_service, "apply_balance_deltas", fail_balance_update)

    with pytest.raises(InjectedFailureError) as excinfo:
        await _withdraw(sessionmaker, funded)

    assert excinfo.value is injected  # the original exception object, not a wrapper
    await _assert_untouched(sessionmaker, funded)
    [event] = await _withdrawal_events(sessionmaker, funded)
    assert (event.outcome, event.reason) == (AuditOutcome.FAILED, "internal_error")
    assert event.transaction_id is None
    assert event.details["error_type"] == "InjectedFailureError"
    # The header had been flushed: its id is recorded, and it was never committed.
    assert not await _transaction_exists(sessionmaker, event.details["attempted_transaction_id"])


async def test_deferred_failure_at_commit_leaves_no_succeeded_event(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The header declares one entry too many, so only the COMMIT-time trigger objects: by
    # then the succeeded audit event has been inserted. It must be rolled back with the rest.
    def miscounted(**values: Any) -> Transaction:
        return Transaction(**{**values, "entry_count": values["entry_count"] + 1})

    monkeypatch.setattr(data_posting, "Transaction", miscounted)

    with pytest.raises(IntegrityError):
        await _withdraw(sessionmaker, funded)

    await _assert_untouched(sessionmaker, funded)
    [event] = await _withdrawal_events(sessionmaker, funded)
    assert event.outcome == AuditOutcome.FAILED
    assert event.details["constraint"] == "ck_transactions_entry_count"
    assert event.details["sqlstate"] == "23514"
    assert not await _transaction_exists(sessionmaker, event.details["attempted_transaction_id"])


async def test_exhausted_retries_are_audited_as_failed_contention(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def deadlock(*args: Any, **kwargs: Any) -> Any:
        raise DBAPIError("SELECT ... FOR UPDATE", None, _DriverError("40P01"))

    monkeypatch.setattr(posting_service, "lock_accounts", deadlock)

    with pytest.raises(TemporarilyUnavailableError):
        await _withdraw(sessionmaker, funded)

    [event] = await _withdrawal_events(sessionmaker, funded)
    assert (event.outcome, event.reason) == (AuditOutcome.FAILED, "temporarily_unavailable")
    assert event.details["sqlstate"] == "40P01"
    assert "attempted_transaction_id" not in event.details  # no attempt got that far


async def test_a_retried_attempt_leaves_exactly_one_succeeded_event(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first attempt writes its success event, then deadlocks: the event is rolled back
    # with the attempt, and only the retry's event commits.
    real_record = audit_service.record_in_transaction
    calls = 0

    async def record_then_deadlock_once(session: AsyncSession, record: AuditRecord) -> None:
        nonlocal calls
        calls += 1
        await real_record(session, record)
        if calls == 1:
            raise DBAPIError("COMMIT", None, _DriverError("40P01"))

    monkeypatch.setattr(posting_service, "record_in_transaction", record_then_deadlock_once)

    result = await _withdraw(sessionmaker, funded)

    assert calls == 2
    [event] = await _withdrawal_events(sessionmaker, funded)
    assert event.outcome == AuditOutcome.SUCCEEDED
    assert event.transaction_id == result.transaction_id


# --- failures of the audit write ---------------------------------------------------------------


async def test_failed_rejection_audit_preserves_the_business_error_and_logs_it(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    test_database_url: URL,
) -> None:
    monkeypatch.setattr(
        audit_service,
        "insert_audit_event",
        _failing_audit_writes(AuditOutcome.REJECTED, error=InjectedFailureError("audit down")),
    )

    with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER), pytest.raises(InsufficientFundsError):
        await _withdraw(sessionmaker, funded, amount=FUNDED + 1)

    await _assert_untouched(sessionmaker, funded)
    assert await _withdrawal_events(sessionmaker, funded) == []
    [log] = [record for record in caplog.records if record.name == AUDIT_LOGGER]
    assert log.levelno == logging.ERROR
    message = log.getMessage()
    assert "action=posting.withdrawal outcome=rejected reason=insufficient_funds" in message
    assert str(funded.account_id) in message  # the log is the fallback audit sink
    assert test_database_url.password is not None
    assert test_database_url.password not in caplog.text


async def test_failed_success_audit_rolls_back_the_posting(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No audit, no money movement: the succeeded event is part of the posting.
    injected = InjectedFailureError("audit insert failed")
    monkeypatch.setattr(
        audit_service,
        "insert_audit_event",
        _failing_audit_writes(AuditOutcome.SUCCEEDED, error=injected),
    )

    with pytest.raises(InjectedFailureError) as excinfo:
        await _withdraw(sessionmaker, funded)

    assert excinfo.value is injected
    await _assert_untouched(sessionmaker, funded)
    [event] = await _withdrawal_events(sessionmaker, funded)
    assert (event.outcome, event.reason) == (AuditOutcome.FAILED, "internal_error")
    assert not await _transaction_exists(sessionmaker, event.details["attempted_transaction_id"])


async def test_when_both_audit_writes_fail_the_original_error_still_wins(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    injected = InjectedFailureError("audit insert failed")
    monkeypatch.setattr(
        audit_service,
        "insert_audit_event",
        _failing_audit_writes(AuditOutcome.SUCCEEDED, AuditOutcome.FAILED, error=injected),
    )

    with (
        caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER),
        pytest.raises(InjectedFailureError) as excinfo,
    ):
        await _withdraw(sessionmaker, funded)

    assert excinfo.value is injected
    await _assert_untouched(sessionmaker, funded)
    assert await _withdrawal_events(sessionmaker, funded) == []
    [log] = [record for record in caplog.records if record.name == AUDIT_LOGGER]
    assert "outcome=failed reason=internal_error" in log.getMessage()


async def test_audit_writer_skips_and_logs_if_called_inside_a_transaction(
    sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    record = AuditRecord(AuditAction.USER_REGISTER, AuditOutcome.REJECTED, reason="anything")
    async with sessionmaker() as session, session.begin():
        before = await session.scalar(select(func.count()).select_from(AuditEvent))
        with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER):
            await audit_service.record_outside_transaction(session, record)  # does not raise
        after = await session.scalar(select(func.count()).select_from(AuditEvent))

    assert after == before
    assert "the session is still in a transaction" in caplog.text
