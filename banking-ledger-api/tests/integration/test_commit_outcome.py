"""Unknown COMMIT outcomes and in-flight retries (ADR 0006, ADR 0010), with real COMMITs.

When COMMIT fails without PostgreSQL saying why (a lost connection), the posting may or may
not have been applied. The service reports outcome_unknown and never claims a failure; the
client retries with the same idempotency key, and the durable key row decides:

- the COMMIT happened, only its acknowledgement was lost: the retry replays it;
- the transaction never committed: the retry runs it, exactly once;
- the original is still committing: the retry's claim WAITS for it, then replays it (or runs,
  if that COMMIT fails). An immediate SELECT at that moment finds no row: inferring "not
  posted" from it would be wrong, and is never done.

Every test checks the persisted state from fresh sessions, not only what the caller was told.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import SessionTransaction

from ledger_api.data import posting as data_posting
from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.models import Account, AuditEvent, IdempotencyKey, Transaction
from ledger_api.data.reconciliation import find_failed_events_for_committed_transactions
from ledger_api.domain.audit import AuditOutcome, AuditRecord
from ledger_api.domain.errors import CommitOutcomeUnknownError
from ledger_api.services import audit as audit_service
from ledger_api.services import posting as posting_service
from ledger_api.services.posting import PostingResult, withdraw
from tests.database import assert_reconciled, audit_events_about
from tests.factories import FundedAccount, commit_funded_account

type Maker = async_sessionmaker[AsyncSession]

FUNDED = 5_000
AMOUNT = 1_000


class _ConnectionLostError(Exception):
    """Stands in for the driver error of a connection that dropped after COMMIT was sent."""


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> Maker:
    return create_sessionmaker(engine)


@pytest.fixture
async def funded(sessionmaker: Maker) -> FundedAccount:
    return await commit_funded_account(sessionmaker, FUNDED)


async def _withdraw(
    sessionmaker: Maker, account: FundedAccount, key: str, session: AsyncSession | None = None
) -> PostingResult:
    async def run(session: AsyncSession) -> PostingResult:
        return await withdraw(
            session,
            owner_id=account.owner_id,
            account_id=account.account_id,
            amount_minor=AMOUNT,
            idempotency_key=key,
        )

    if session is not None:
        return await run(session)
    async with sessionmaker() as fresh:
        return await run(fresh)


async def _key_row(sessionmaker: Maker, account: FundedAccount, key: str) -> IdempotencyKey | None:
    async with sessionmaker() as session:
        return await session.get(IdempotencyKey, (account.owner_id, key))


async def _balance(sessionmaker: Maker, account: FundedAccount) -> int:
    async with sessionmaker() as session:
        balance = await session.scalar(
            select(Account.balance_minor).where(Account.id == account.account_id)
        )
    assert balance is not None
    return balance


async def _transaction_exists(sessionmaker: Maker, transaction_id: uuid.UUID) -> bool:
    async with sessionmaker() as session:
        return await session.get(Transaction, transaction_id) is not None


async def _withdrawal_events(sessionmaker: Maker, account: FundedAccount) -> list[AuditEvent]:
    async with sessionmaker() as session:
        funding, *rest = await audit_events_about(session, account.account_id)
    assert funding.outcome == AuditOutcome.SUCCEEDED
    return rest


async def _assert_reconciled(sessionmaker: Maker) -> None:
    async with sessionmaker() as session:
        await assert_reconciled(session)


# --- a. COMMIT succeeded, the acknowledgement was lost -------------------------------------------


async def test_lost_acknowledgement_is_outcome_unknown_and_the_retry_replays(
    sessionmaker: Maker, funded: FundedAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = f"lost-ack-{uuid.uuid7().hex}"
    real_commit = SessionTransaction.commit

    async with sessionmaker() as session:
        target = session.sync_session

        def commit_then_lose_the_acknowledgement(self: SessionTransaction, *args: Any) -> None:
            real_commit(self, *args)  # PostgreSQL has committed...
            if self.session is target and self.parent is None:
                monkeypatch.setattr(SessionTransaction, "commit", real_commit)  # once
                # ...but the caller never hears it: the connection dropped.
                raise DBAPIError(
                    "COMMIT", None, _ConnectionLostError(), connection_invalidated=True
                )

        monkeypatch.setattr(SessionTransaction, "commit", commit_then_lose_the_acknowledgement)
        with pytest.raises(CommitOutcomeUnknownError) as excinfo:
            await _withdraw(sessionmaker, funded, key, session)

    cause = excinfo.value.__cause__
    assert isinstance(cause, DBAPIError)
    assert cause.connection_invalidated
    # The persisted truth: the posting committed, with its key and its succeeded event.
    row = await _key_row(sessionmaker, funded, key)
    assert row is not None
    assert await _transaction_exists(sessionmaker, row.transaction_id)
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT
    succeeded, unknown = await _withdrawal_events(sessionmaker, funded)
    assert (succeeded.outcome, succeeded.transaction_id) == (
        AuditOutcome.SUCCEEDED,
        row.transaction_id,
    )
    # What the caller was told is recorded too, and reconciliation flags the discrepancy.
    assert (unknown.outcome, unknown.reason) == (AuditOutcome.FAILED, "outcome_unknown")
    assert unknown.idempotency_key == key
    assert unknown.details["attempted_transaction_id"] == str(row.transaction_id)
    assert unknown.details["connection_invalidated"] is True
    async with sessionmaker() as session:
        assert unknown.id in await find_failed_events_for_committed_transactions(session)

    retried = await _withdraw(sessionmaker, funded, key)

    assert retried.replayed
    assert retried.transaction_id == row.transaction_id
    assert retried.balances == {funded.account_id: FUNDED - AMOUNT}
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT  # applied exactly once
    await _assert_reconciled(sessionmaker)


async def test_a_non_database_error_at_commit_is_unknown_and_audited_with_its_type_only(
    sessionmaker: Maker,
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A raw socket error from the COMMIT path (no DBAPIError around it): the outcome is unknown,
    # and the audit keeps what caused it by class name, never by message.
    connection_details = "connection to 10.0.0.9:5432 as ledger_app reset by peer"
    key = f"reset-{uuid.uuid7().hex}"
    real_commit = SessionTransaction.commit

    async with sessionmaker() as session:
        target = session.sync_session

        def reset_instead_of_committing(self: SessionTransaction, *args: Any) -> None:
            if self.session is target and self.parent is None:
                monkeypatch.setattr(SessionTransaction, "commit", real_commit)  # once
                raise ConnectionResetError(connection_details)  # COMMIT never reached the server
            real_commit(self, *args)

        monkeypatch.setattr(SessionTransaction, "commit", reset_instead_of_committing)
        with caplog.at_level(logging.DEBUG), pytest.raises(CommitOutcomeUnknownError) as excinfo:
            await _withdraw(sessionmaker, funded, key, session)

    assert isinstance(excinfo.value.__cause__, ConnectionResetError)
    # Nothing committed (the session rolled back), and the record says what the caller saw.
    assert await _key_row(sessionmaker, funded, key) is None
    assert await _balance(sessionmaker, funded) == FUNDED
    [unknown] = await _withdrawal_events(sessionmaker, funded)
    assert (unknown.outcome, unknown.reason) == (AuditOutcome.FAILED, "outcome_unknown")
    assert unknown.details["error_type"] == "CommitOutcomeUnknownError"
    assert unknown.details["cause_type"] == "ConnectionResetError"
    assert "sqlstate" not in unknown.details
    assert connection_details not in str(unknown.details)
    assert connection_details not in caplog.text

    retried = await _withdraw(sessionmaker, funded, key)

    assert not retried.replayed
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT
    await _assert_reconciled(sessionmaker)


# --- b. the transaction definitely did not commit ------------------------------------------------


async def test_connection_lost_before_commit_is_outcome_unknown_and_the_retry_runs_once(
    sessionmaker: Maker,
    engine: AsyncEngine,
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real connection loss through the real driver: the backend is terminated after the
    # posting's last write, so the COMMIT is sent on a dead connection. PostgreSQL aborts a
    # terminated backend's open transaction: nothing can have been committed. The caller cannot
    # know that, so it is told outcome_unknown, and the durable record shows the truth.
    key = f"terminated-{uuid.uuid7().hex}"
    real_record = audit_service.record_in_transaction
    terminated: list[int] = []

    async def record_then_lose_the_connection(session: AsyncSession, record: AuditRecord) -> None:
        await real_record(session, record)
        if terminated:
            return
        pid = await session.scalar(select(func.pg_backend_pid()))
        assert isinstance(pid, int)
        terminated.append(pid)
        async with engine.connect() as admin:
            assert await admin.scalar(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid})

    monkeypatch.setattr(posting_service, "record_in_transaction", record_then_lose_the_connection)

    with pytest.raises(CommitOutcomeUnknownError) as excinfo:
        await _withdraw(sessionmaker, funded, key)

    assert terminated
    cause = excinfo.value.__cause__
    assert isinstance(cause, DBAPIError)
    assert cause.connection_invalidated  # the driver saw the connection die
    # The persisted truth: nothing committed. No key, no transaction, no balance change.
    assert await _key_row(sessionmaker, funded, key) is None
    assert await _balance(sessionmaker, funded) == FUNDED
    [unknown] = await _withdrawal_events(sessionmaker, funded)
    assert (unknown.outcome, unknown.reason) == (AuditOutcome.FAILED, "outcome_unknown")
    attempted = uuid.UUID(unknown.details["attempted_transaction_id"])
    assert not await _transaction_exists(sessionmaker, attempted)

    retried = await _withdraw(sessionmaker, funded, key)

    assert not retried.replayed  # nothing to replay: it runs now
    assert retried.transaction_id != attempted
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT  # exactly once
    row = await _key_row(sessionmaker, funded, key)
    assert row is not None
    assert row.transaction_id == retried.transaction_id
    await _assert_reconciled(sessionmaker)


async def test_a_commit_refused_by_postgresql_is_not_reported_as_unknown(
    sessionmaker: Maker, funded: FundedAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PostgreSQL answered the COMMIT with an error on a live connection: definitely rolled
    # back, so the existing handling applies (500 internal_error), not outcome_unknown.
    def miscounted(**values: Any) -> Transaction:
        return Transaction(**{**values, "entry_count": values["entry_count"] + 1})

    monkeypatch.setattr(data_posting, "Transaction", miscounted)
    key = f"refused-{uuid.uuid7().hex}"

    with pytest.raises(IntegrityError):
        await _withdraw(sessionmaker, funded, key)

    assert await _key_row(sessionmaker, funded, key) is None
    [failed] = await _withdrawal_events(sessionmaker, funded)
    assert (failed.outcome, failed.reason) == (AuditOutcome.FAILED, "internal_error")


async def _reject_commit_with(session: AsyncSession, code: str) -> None:
    """Make PostgreSQL itself refuse this transaction's COMMIT with SQLSTATE `code`.

    A deferred constraint trigger on a temporary table, all of it private to the transaction
    (ON COMMIT DROP; rolled back with it), fires during COMMIT processing and raises.
    """
    await session.execute(text("CREATE TEMP TABLE commit_probe (x int) ON COMMIT DROP"))
    await session.execute(
        text(
            "CREATE FUNCTION pg_temp.commit_probe_fail() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN RAISE EXCEPTION 'test: COMMIT refused' USING ERRCODE = '{code}'; END $$"
        )
    )
    await session.execute(
        text(
            "CREATE CONSTRAINT TRIGGER commit_probe AFTER INSERT ON commit_probe "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION pg_temp.commit_probe_fail()"
        )
    )
    await session.execute(text("INSERT INTO commit_probe VALUES (1)"))


def _refuse_the_next_commit(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    real_record = audit_service.record_in_transaction
    armed = True

    async def record_then_arm(session: AsyncSession, record: AuditRecord) -> None:
        nonlocal armed
        await real_record(session, record)
        if armed:  # the first posting attempt only; its retry commits normally
            armed = False
            await _reject_commit_with(session, code)

    monkeypatch.setattr(posting_service, "record_in_transaction", record_then_arm)


@pytest.mark.parametrize("code", ["42501", "P0001", "22003", "53100", "XX000", "23514"])
async def test_a_commit_postgresql_refused_on_a_live_connection_is_a_definite_failure(
    sessionmaker: Maker, funded: FundedAccount, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    # Whatever the SQLSTATE: the server answered COMMIT with an ERROR and kept the session, so
    # it rolled back. Reported as the failure it is (500), never as outcome_unknown.
    _refuse_the_next_commit(monkeypatch, code)
    key = f"refused-{code}-{uuid.uuid7().hex}"

    with pytest.raises(DBAPIError) as excinfo:
        await _withdraw(sessionmaker, funded, key)

    error = excinfo.value
    assert not isinstance(error, CommitOutcomeUnknownError)
    driver_error = error.orig.__cause__ if error.orig is not None else None
    assert (
        getattr(driver_error, "sqlstate", None),
        getattr(driver_error, "severity_en", None),
    ) == (
        code,
        "ERROR",
    )
    assert not error.connection_invalidated
    # The persisted truth agrees: nothing committed.
    assert await _key_row(sessionmaker, funded, key) is None
    assert await _balance(sessionmaker, funded) == FUNDED
    [failed] = await _withdrawal_events(sessionmaker, funded)
    assert (failed.outcome, failed.reason) == (AuditOutcome.FAILED, "internal_error")
    assert failed.details["sqlstate"] == code
    assert not await _transaction_exists(
        sessionmaker, uuid.UUID(failed.details["attempted_transaction_id"])
    )

    retried = await _withdraw(sessionmaker, funded, key)  # the trigger is armed only once

    assert not retried.replayed
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT
    await _assert_reconciled(sessionmaker)


async def test_statement_completion_unknown_at_commit_is_outcome_unknown(
    sessionmaker: Maker, funded: FundedAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 40003 says, by definition, that the outcome is unknown: never a definite rollback, even
    # as a non-fatal ERROR on a live connection. (Here the server did roll back; the caller is
    # told the truth it has, and the durable record settles it on retry.)
    _refuse_the_next_commit(monkeypatch, "40003")
    key = f"completion-unknown-{uuid.uuid7().hex}"

    with pytest.raises(CommitOutcomeUnknownError) as excinfo:
        await _withdraw(sessionmaker, funded, key)

    cause = excinfo.value.__cause__
    assert isinstance(cause, DBAPIError)
    assert cause.orig is not None
    assert getattr(cause.orig.__cause__, "sqlstate", None) == "40003"
    assert not cause.connection_invalidated
    assert await _key_row(sessionmaker, funded, key) is None
    [unknown] = await _withdrawal_events(sessionmaker, funded)
    assert (unknown.outcome, unknown.reason) == (AuditOutcome.FAILED, "outcome_unknown")
    assert unknown.details["sqlstate"] == "40003"

    retried = await _withdraw(sessionmaker, funded, key)

    assert not retried.replayed  # nothing had committed: the retry runs, once
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT
    await _assert_reconciled(sessionmaker)


# --- c. a retry while the original is still committing -------------------------------------------

SLOW_COMMIT_PREFIX = "slow-commit-"
SLOW_COMMIT_SECONDS = 1.0  # well inside lock_timeout (2 s), which bounds the retry's wait
# Set (SET LOCAL) by a transaction whose slow COMMIT must then fail.
FAIL_SLOW_COMMIT = "ledger_test.fail_slow_commit"


@pytest.fixture
async def slow_commits(engine: AsyncEngine) -> AsyncIterator[None]:
    """TEST-ONLY: keys with SLOW_COMMIT_PREFIX take SLOW_COMMIT_SECONDS to COMMIT, server side.

    A deferred constraint trigger runs inside COMMIT processing, so for that long the backend
    is "still finishing COMMIT": its key row is not visible to anyone, yet the transaction has
    not ended. A transaction that SET LOCAL FAIL_SLOW_COMMIT = 'on' then has its COMMIT refused.
    Dropped afterwards; other keys are never affected.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"""
                CREATE FUNCTION test_slow_commit() RETURNS trigger
                LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
                BEGIN
                    PERFORM pg_sleep({SLOW_COMMIT_SECONDS});
                    IF current_setting('{FAIL_SLOW_COMMIT}', true) = 'on' THEN
                        RAISE EXCEPTION 'test: failing a slow COMMIT'
                            USING ERRCODE = 'check_violation', CONSTRAINT = 'test_slow_commit';
                    END IF;
                    RETURN NULL;
                END;
                $$
                """
            )
        )
        await connection.execute(
            text(
                "CREATE CONSTRAINT TRIGGER test_slow_commit AFTER INSERT ON idempotency_keys "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                f"WHEN (NEW.key LIKE '{SLOW_COMMIT_PREFIX}%') "
                "EXECUTE FUNCTION test_slow_commit()"
            )
        )
    try:
        yield
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TRIGGER test_slow_commit ON idempotency_keys"))
            await connection.execute(text("DROP FUNCTION test_slow_commit()"))


async def _wait_until_committing(engine: AsyncEngine) -> None:
    """Until some backend is inside a COMMIT, sleeping in the test trigger."""
    deadline = time.monotonic() + 5
    async with engine.connect() as observer:
        while time.monotonic() < deadline:
            committing = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    # asyncpg sends "COMMIT;"
                    "WHERE state = 'active' AND query LIKE 'COMMIT%' AND wait_event = 'PgSleep'"
                )
            )
            await observer.rollback()  # a fresh snapshot of pg_stat_activity each time
            if committing:
                return
            await asyncio.sleep(0.02)
    pytest.fail("the original posting never reached its COMMIT")


async def _visible_key_rows(engine: AsyncEngine, account: FundedAccount, key: str) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(
            select(func.count()).where(
                IdempotencyKey.user_id == account.owner_id, IdempotencyKey.key == key
            )
        )
    return count or 0


async def _timed(coroutine: Any) -> tuple[Any, float]:
    try:
        result = await coroutine
    except Exception as error:  # inspected by the test
        result = error
    return result, time.monotonic()


@pytest.mark.concurrency
@pytest.mark.usefixtures("slow_commits")
async def test_a_retry_waits_for_a_commit_in_progress_and_replays_it(
    sessionmaker: Maker, engine: AsyncEngine, funded: FundedAccount
) -> None:
    key = f"{SLOW_COMMIT_PREFIX}{uuid.uuid7().hex}"
    original = asyncio.create_task(_timed(_withdraw(sessionmaker, funded, key)))
    await _wait_until_committing(engine)

    # The original has sent COMMIT and not finished it. A SELECT sees nothing: "no row" here
    # does NOT mean "not posted".
    assert await _visible_key_rows(engine, funded, key) == 0

    # The client gives up and retries with the same key. Its claim waits for the original...
    (retried, retried_at) = await _timed(_withdraw(sessionmaker, funded, key))
    (first, first_at) = await original

    # ...which committed; the retry then replays it.
    assert isinstance(first, PostingResult)
    assert isinstance(retried, PostingResult)
    assert not first.replayed
    assert retried.replayed
    assert retried.transaction_id == first.transaction_id
    assert retried.balances == first.balances == {funded.account_id: FUNDED - AMOUNT}
    assert retried_at >= first_at  # it could only answer once the original had ended
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT  # exactly once
    await _assert_reconciled(sessionmaker)


@pytest.mark.concurrency
@pytest.mark.usefixtures("slow_commits")
async def test_a_retry_waits_for_a_commit_in_progress_and_runs_if_that_commit_fails(
    sessionmaker: Maker,
    engine: AsyncEngine,
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_record = audit_service.record_in_transaction
    calls = 0

    async def fail_the_first_commit(session: AsyncSession, record: AuditRecord) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:  # the original's transaction only
            await session.execute(text(f"SET LOCAL {FAIL_SLOW_COMMIT} = 'on'"))
        await real_record(session, record)

    monkeypatch.setattr(posting_service, "record_in_transaction", fail_the_first_commit)
    key = f"{SLOW_COMMIT_PREFIX}{uuid.uuid7().hex}"
    original = asyncio.create_task(_timed(_withdraw(sessionmaker, funded, key)))
    await _wait_until_committing(engine)
    assert await _visible_key_rows(engine, funded, key) == 0

    (retried, retried_at) = await _timed(_withdraw(sessionmaker, funded, key))
    (first, first_at) = await original

    # PostgreSQL refused the original's COMMIT (definitely rolled back, so not "unknown"), its
    # claim vanished with it, and the waiting retry's own claim went ahead: it posted, once.
    assert isinstance(first, IntegrityError)
    assert isinstance(retried, PostingResult)
    assert not retried.replayed
    assert retried_at >= first_at
    row = await _key_row(sessionmaker, funded, key)
    assert row is not None
    assert row.transaction_id == retried.transaction_id
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT  # exactly once
    outcomes = [
        (event.outcome, event.reason) for event in await _withdrawal_events(sessionmaker, funded)
    ]
    assert outcomes == [(AuditOutcome.FAILED, "internal_error"), (AuditOutcome.SUCCEEDED, None)]
    await _assert_reconciled(sessionmaker)
