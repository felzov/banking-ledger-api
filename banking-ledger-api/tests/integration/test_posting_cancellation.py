"""A posting whose request is cancelled (Phase 9 A4), with real COMMITs on real connections.

asyncio.CancelledError is not an Exception: it bypasses the posting's error classification and
its failure audit, and always reaches the caller. What protects the ledger is the transaction
itself. Cancelled before COMMIT, nothing is committed: SQLAlchemy rolls back, or ends the
backend and PostgreSQL rolls back. Cancelled after PostgreSQL committed, the posting and its
success event stand together. Either way, a retry with the same Idempotency-Key never posts
twice: it runs once, or replays.

Every pause is an explicit synchronisation point (an Event inside a hooked step, or a state
polled on the server, with a bound); no test relies on a sleep being long enough.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Collection

import pytest
from sqlalchemy import URL, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_api.data import engine as data_engine
from ledger_api.data.audit import insert_audit_event
from ledger_api.data.engine import create_engine, create_sessionmaker
from ledger_api.data.models import AuditEvent
from ledger_api.data.posting import LockedAccount, lock_accounts
from ledger_api.data.statements import PostedEntry, find_posted_entry
from ledger_api.domain.audit import AuditOutcome, AuditRecord
from ledger_api.services import audit as audit_service
from ledger_api.services import posting as posting_service
from ledger_api.services.posting import PostingResult
from tests.conftest import settings_for
from tests.factories import FundedAccount, commit_funded_account
from tests.integration.test_commit_outcome import (
    AMOUNT,
    FUNDED,
    Maker,
    _assert_reconciled,
    _balance,
    _key_row,
    _withdraw,
    _withdrawal_events,
)
from tests.tcp_proxy import ReplyHoldingProxy

POSTING_LOGGER = "ledger_api.services.posting"
AUDIT_LOGGER = "ledger_api.services.audit"
APPLICATION_NAME = "banking-ledger-api"  # data.engine sets it on every connection


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> Maker:
    return create_sessionmaker(engine)


@pytest.fixture
async def funded(sessionmaker: Maker) -> FundedAccount:
    return await commit_funded_account(sessionmaker, FUNDED)


async def _eventually(check: Callable[[], Awaitable[bool]], what: str) -> None:
    """Poll `check` (each call a fresh snapshot) until it holds; fail, never pass, if it does
    not within the bound."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"never happened: {what}")


async def _backends(sessionmaker: Maker) -> list[tuple[int, str, str | None]]:
    """(pid, state, wait_event_type) of every other application backend on this database."""
    async with sessionmaker() as session:
        rows = await session.execute(
            text(
                "SELECT pid, state, wait_event_type FROM pg_stat_activity"
                " WHERE datname = current_database() AND application_name = :name"
                " AND pid <> pg_backend_pid()"
            ),
            {"name": APPLICATION_NAME},
        )
        return [(pid, state, wait) for pid, state, wait in rows]


async def _assert_no_transaction_left_open(sessionmaker: Maker, engine: AsyncEngine) -> None:
    assert engine.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    leftover = [backend for backend in await _backends(sessionmaker) if backend[1] != "idle"]
    assert leftover == []  # nothing idle in transaction, nothing still running or waiting


def _posting_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == POSTING_LOGGER and record.levelno == logging.WARNING
    ]


def _start(sessionmaker: Maker, funded: FundedAccount, key: str) -> asyncio.Task[PostingResult]:
    return asyncio.create_task(_withdraw(sessionmaker, funded, key))


async def test_a_cancellation_after_the_key_claim_commits_nothing(
    sessionmaker: Maker,
    engine: AsyncEngine,
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = f"cancel-before-commit-{uuid.uuid7().hex}"
    claimed = asyncio.Event()

    async def wait_after_the_claim(
        session: AsyncSession, account_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, LockedAccount]:
        claimed.set()  # the key claim (the first write) is done, uncommitted
        await asyncio.Event().wait()  # until cancelled
        raise AssertionError("unreachable")

    monkeypatch.setattr(posting_service, "lock_accounts", wait_after_the_claim)
    task = _start(sessionmaker, funded, key)
    await claimed.wait()

    with caplog.at_level(logging.WARNING, logger=POSTING_LOGGER):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Nothing committed: no key, no balance change, no event (cancellation is not audited).
    assert await _key_row(sessionmaker, funded, key) is None
    assert await _balance(sessionmaker, funded) == FUNDED
    assert await _withdrawal_events(sessionmaker, funded) == []
    assert _posting_warnings(caplog) == []  # not during COMMIT: nothing to warn about
    await _assert_no_transaction_left_open(sessionmaker, engine)

    monkeypatch.setattr(posting_service, "lock_accounts", lock_accounts)
    retried = await _withdraw(sessionmaker, funded, key)

    assert not retried.replayed  # nothing to replay: it runs now, once
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT
    await _assert_reconciled(sessionmaker)


@pytest.mark.concurrency
async def test_a_cancellation_while_waiting_for_a_row_lock_commits_nothing(
    sessionmaker: Maker,
    test_database_url: URL,
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Timeouts far above the test's own bound: the cancellation, never a lock or statement
    # timeout, must be what ends the wait.
    monkeypatch.setattr(data_engine, "LOCK_TIMEOUT_MS", 60_000)
    monkeypatch.setattr(data_engine, "STATEMENT_TIMEOUT_MS", 60_000)
    patient = create_engine(settings_for(test_database_url))
    patient_sessions = create_sessionmaker(patient)
    key = f"cancel-lock-wait-{uuid.uuid7().hex}"
    try:
        async with sessionmaker() as blocker, blocker.begin():
            await blocker.execute(
                text("SELECT 1 FROM accounts WHERE id = :id FOR UPDATE"),
                {"id": funded.account_id},
            )
            blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
            task = _start(patient_sessions, funded, key)

            async def waiting() -> bool:
                return any(
                    pid != blocker_pid and wait == "Lock"
                    for pid, _, wait in await _backends(sessionmaker)
                )

            await _eventually(waiting, "the posting waits for the account's row lock")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # The lock is still held, yet the cancelled posting is no longer waiting for it:
            # it cannot acquire it later and carry on.
            assert not await waiting()
        # The blocker has rolled back: the lock is free.

        assert await _key_row(sessionmaker, funded, key) is None
        assert await _balance(sessionmaker, funded) == FUNDED
        assert await _withdrawal_events(sessionmaker, funded) == []
        await _assert_no_transaction_left_open(sessionmaker, patient)
    finally:
        await patient.dispose()

    retried = await _withdraw(sessionmaker, funded, key)

    assert not retried.replayed
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT
    await _assert_reconciled(sessionmaker)


@pytest.mark.concurrency
async def test_a_cancellation_after_postgresql_committed_keeps_the_posting(
    sessionmaker: Maker,
    test_database_url: URL,
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The posting runs through a proxy that, after the posting's last write, withholds
    # PostgreSQL's replies: the COMMIT reaches the server and is applied, its acknowledgement
    # does not reach the client. The client is cancelled in exactly that state.
    assert test_database_url.host is not None
    assert test_database_url.port is not None
    key = f"cancel-during-commit-{uuid.uuid7().hex}"
    real_record = audit_service.record_in_transaction

    async with ReplyHoldingProxy(test_database_url.host, test_database_url.port) as proxy:
        proxied = create_engine(
            settings_for(test_database_url.set(host="127.0.0.1", port=proxy.port))
        )

        async def record_then_hold_replies(session: AsyncSession, record: AuditRecord) -> None:
            await real_record(session, record)  # the last write, acknowledged
            proxy.hold_replies()  # COMMIT goes out; its acknowledgement will be held

        monkeypatch.setattr(posting_service, "record_in_transaction", record_then_hold_replies)
        try:
            task = _start(create_sessionmaker(proxied), funded, key)

            async def committed() -> bool:
                return await _key_row(sessionmaker, funded, key) is not None

            # Seen from another connection: PostgreSQL has committed. The client has not
            # heard: its task is still waiting for the acknowledgement.
            await _eventually(committed, "PostgreSQL applies the COMMIT")
            assert not task.done()

            with caplog.at_level(logging.WARNING, logger=POSTING_LOGGER):
                task.cancel()
                proxy.release_replies()
                with pytest.raises(asyncio.CancelledError):
                    async with asyncio.timeout(10):
                        await task

            assert proxied.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        finally:
            await proxied.dispose()

    # The posting outlived its request: committed, with its success event, exactly once.
    row = await _key_row(sessionmaker, funded, key)
    assert row is not None
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT
    [succeeded] = await _withdrawal_events(sessionmaker, funded)
    assert (succeeded.outcome, succeeded.transaction_id) == (
        AuditOutcome.SUCCEEDED,
        row.transaction_id,
    )
    # One WARNING, naming the transaction, never the key.
    [warning] = _posting_warnings(caplog)
    assert "cancelled during COMMIT" in warning
    assert str(row.transaction_id) in warning
    assert key not in warning

    retried = await _withdraw(sessionmaker, funded, key)

    assert retried.replayed  # the same posting, not a second one
    assert retried.transaction_id == row.transaction_id
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT
    await _assert_reconciled(sessionmaker)


async def test_a_cancelled_rejection_audit_is_logged_as_lost(
    sessionmaker: Maker,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    poor = await commit_funded_account(sessionmaker, AMOUNT - 1)  # every withdrawal is refused
    key = f"cancel-rejection-audit-{uuid.uuid7().hex}"
    writing = asyncio.Event()
    real_insert = insert_audit_event

    async def wait_while_writing_rejections(session: AsyncSession, record: AuditRecord) -> None:
        if record.outcome != AuditOutcome.SUCCEEDED:
            writing.set()
            await asyncio.Event().wait()  # until cancelled
        await real_insert(session, record)

    monkeypatch.setattr(audit_service, "insert_audit_event", wait_while_writing_rejections)
    task = _start(sessionmaker, poor, key)
    await writing.wait()  # the posting was rejected and rolled back; its event is being written

    with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):  # the cancellation, not the rejection
            await task

    # The rejected event was never written, and the log says so instead of staying silent.
    async with sessionmaker() as session:
        events = (
            await session.scalars(select(AuditEvent).where(AuditEvent.idempotency_key == key))
        ).all()
    assert events == []
    [lost] = [r.getMessage() for r in caplog.records if r.name == AUDIT_LOGGER]
    assert lost.startswith("audit event not written (cancelled)")
    assert "outcome=rejected reason=insufficient_funds" in lost
    # The rejection itself changed nothing.
    assert await _key_row(sessionmaker, poor, key) is None
    assert await _balance(sessionmaker, poor) == AMOUNT - 1
    await _assert_no_transaction_left_open(sessionmaker, engine)
    await _assert_reconciled(sessionmaker)


def _proxied(test_database_url: URL, proxy: ReplyHoldingProxy) -> AsyncEngine:
    return create_engine(settings_for(test_database_url.set(host="127.0.0.1", port=proxy.port)))


async def _events_with_key(sessionmaker: Maker, key: str) -> list[AuditEvent]:
    async with sessionmaker() as session:
        statement = select(AuditEvent).where(AuditEvent.idempotency_key == key)
        return list((await session.scalars(statement)).all())


@pytest.mark.concurrency
async def test_a_rejection_audit_cancelled_during_its_commit_is_logged_as_uncertain(
    sessionmaker: Maker,
    test_database_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The rejected event's own COMMIT reaches PostgreSQL and is applied; its acknowledgement is
    # withheld and the request cancelled. The event exists, so the log must not say it does not.
    assert test_database_url.host is not None
    assert test_database_url.port is not None
    poor = await commit_funded_account(sessionmaker, AMOUNT - 1)  # every withdrawal is refused
    key = f"cancel-rejection-audit-commit-{uuid.uuid7().hex}"

    async with ReplyHoldingProxy(test_database_url.host, test_database_url.port) as proxy:
        proxied = _proxied(test_database_url, proxy)

        async def insert_then_hold_replies(session: AsyncSession, record: AuditRecord) -> None:
            await insert_audit_event(session, record)  # acknowledged
            if record.outcome != AuditOutcome.SUCCEEDED:
                proxy.hold_replies()  # its COMMIT goes out; the acknowledgement is held

        monkeypatch.setattr(audit_service, "insert_audit_event", insert_then_hold_replies)
        try:
            task = _start(create_sessionmaker(proxied), poor, key)

            async def written() -> bool:
                return await _events_with_key(sessionmaker, key) != []

            await _eventually(written, "PostgreSQL commits the rejected event")
            assert not task.done()  # the client has not heard

            with caplog.at_level(logging.ERROR, logger=AUDIT_LOGGER):
                task.cancel()
                proxy.release_replies()
                with pytest.raises(asyncio.CancelledError):
                    async with asyncio.timeout(10):
                        await task

            assert proxied.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        finally:
            await proxied.dispose()

    [event] = await _events_with_key(sessionmaker, key)
    assert (event.outcome, event.reason) == (AuditOutcome.REJECTED, "insufficient_funds")
    [uncertain] = [r.getMessage() for r in caplog.records if r.name == AUDIT_LOGGER]
    assert uncertain.startswith("audit event possibly not written (cancelled during COMMIT)")
    assert await _balance(sessionmaker, poor) == AMOUNT - 1
    await _assert_reconciled(sessionmaker)


@pytest.mark.concurrency
async def test_a_replay_cancelled_during_its_commit_logs_no_warning(
    sessionmaker: Maker,
    test_database_url: URL,
    funded: FundedAccount,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A replay's transaction writes nothing, so a cancellation during its COMMIT leaves no
    # outcome in doubt: no "outcome unknown" warning, and still exactly one posting.
    assert test_database_url.host is not None
    assert test_database_url.port is not None
    key = f"cancel-replay-commit-{uuid.uuid7().hex}"
    original = await _withdraw(sessionmaker, funded, key)
    backend: list[int] = []

    async with ReplyHoldingProxy(test_database_url.host, test_database_url.port) as proxy:
        proxied = _proxied(test_database_url, proxy)

        async def find_then_hold_replies(
            session: AsyncSession, *, transaction_id: uuid.UUID, account_id: uuid.UUID
        ) -> PostedEntry | None:
            entry = await find_posted_entry(
                session, transaction_id=transaction_id, account_id=account_id
            )
            backend.append(await session.scalar(text("SELECT pg_backend_pid()")))
            proxy.hold_replies()  # the replay's COMMIT goes out; the acknowledgement is held
            return entry

        monkeypatch.setattr(posting_service, "find_posted_entry", find_then_hold_replies)
        try:
            task = _start(create_sessionmaker(proxied), funded, key)

            async def committed() -> bool:
                if not backend:
                    return False
                async with sessionmaker() as session:
                    state = await session.scalar(
                        text("SELECT state FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": backend[0]},
                    )
                # No longer "idle in transaction": PostgreSQL has applied the COMMIT.
                return isinstance(state, str) and state == "idle"

            await _eventually(committed, "PostgreSQL ends the replay's transaction")
            assert not task.done()

            with caplog.at_level(logging.WARNING, logger=POSTING_LOGGER):
                task.cancel()
                proxy.release_replies()
                with pytest.raises(asyncio.CancelledError):
                    async with asyncio.timeout(10):
                        await task

            assert proxied.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        finally:
            await proxied.dispose()

    assert _posting_warnings(caplog) == []
    # Still the one original posting, debited once.
    row = await _key_row(sessionmaker, funded, key)
    assert row is not None
    assert row.transaction_id == original.transaction_id
    assert await _balance(sessionmaker, funded) == FUNDED - AMOUNT
    [succeeded] = await _withdrawal_events(sessionmaker, funded)
    assert succeeded.transaction_id == original.transaction_id
    await _assert_reconciled(sessionmaker)
