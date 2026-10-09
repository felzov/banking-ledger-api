"""audit_events: constraints, append-only protection and actor resolution (ADR 0011).

Every test runs on the rolled-back test connection.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.audit import insert_audit_event
from ledger_api.data.errors import sqlstate
from ledger_api.data.models import AuditEvent
from ledger_api.domain.audit import AuditAction, AuditOutcome, AuditRecord
from tests.database import bypass_triggers, check_deferred_constraints, raises_violation
from tests.factories import add_entries, create_customer_account, create_transaction, create_user

REJECTED = {"action": "account.open", "outcome": "rejected", "reason": "user_not_found"}


async def _insert(session: AsyncSession, **values: Any) -> int:
    """Raw INSERT, bypassing AuditRecord: the database must enforce the rules by itself."""
    statement = insert(AuditEvent).values(**{**REJECTED, **values}).returning(AuditEvent.id)
    event_id = await session.scalar(statement)
    assert event_id is not None
    return event_id


async def _posted_transaction_id(session: AsyncSession) -> uuid.UUID:
    payer = await create_customer_account(session, balance_minor=100)
    payee = await create_customer_account(session)
    transaction = await create_transaction(session)
    add_entries(session, transaction, (payer, -100), (payee, 100))
    await check_deferred_constraints(session)
    return transaction.id


# --- accepted events ---------------------------------------------------------------------------


async def test_successful_posting_event_is_accepted(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    transaction_id = await _posted_transaction_id(db_session)

    event_id = await _insert(
        db_session,
        action="posting.transfer",
        outcome="succeeded",
        reason=None,
        actor_user_id=user.id,
        transaction_id=transaction_id,
        details={"amount_minor": 100},
    )

    event = await db_session.get(AuditEvent, event_id)
    assert event is not None
    assert event.occurred_at is not None
    assert event.request_id is None
    assert event.idempotency_key is None


async def test_details_default_to_an_empty_object(db_session: AsyncSession) -> None:
    event_id = await _insert(db_session)

    details = await db_session.scalar(select(AuditEvent.details).where(AuditEvent.id == event_id))

    assert details == {}


async def test_ids_are_generated_by_the_database_only(db_session: AsyncSession) -> None:
    # GENERATED ALWAYS: a client-chosen id is refused (428C9 generated_always).
    with pytest.raises(DBAPIError) as excinfo:
        await _insert(db_session, id=1)

    assert sqlstate(excinfo.value) == "428C9"


# --- CHECK constraints -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"action": "account.close"}, "ck_audit_events_action"),
        ({"outcome": "pending"}, "ck_audit_events_outcome"),
        ({"reason": None}, "ck_audit_events_reason_iff_unsuccessful"),
        ({"reason": "Not A Code"}, "ck_audit_events_reason_format"),
        ({"details": [1, 2]}, "ck_audit_events_details_object"),
        ({"details": {"blob": "x" * 2048}}, "ck_audit_events_details_size"),
        ({"request_id": ""}, "ck_audit_events_request_id_length"),
        ({"request_id": "r" * 129}, "ck_audit_events_request_id_length"),
        ({"idempotency_key": ""}, "ck_audit_events_idempotency_key_length"),
        ({"idempotency_key": "k" * 256}, "ck_audit_events_idempotency_key_length"),
    ],
)
async def test_invalid_event_is_rejected(
    db_session: AsyncSession, values: dict[str, Any], constraint: str
) -> None:
    async with raises_violation(constraint):
        await _insert(db_session, **values)


async def test_success_must_not_carry_a_reason(db_session: AsyncSession) -> None:
    user = await create_user(db_session)

    async with raises_violation("ck_audit_events_reason_iff_unsuccessful"):
        await _insert(db_session, outcome="succeeded", actor_user_id=user.id)


async def test_successful_posting_requires_its_transaction(db_session: AsyncSession) -> None:
    user = await create_user(db_session)

    async with raises_violation("ck_audit_events_transaction_iff_posted"):
        await _insert(
            db_session,
            action="posting.deposit",
            outcome="succeeded",
            reason=None,
            actor_user_id=user.id,
        )


@pytest.mark.parametrize(
    "values",
    [
        {"action": "posting.deposit", "outcome": "rejected", "reason": "insufficient_funds"},
        {"action": "posting.deposit", "outcome": "failed", "reason": "internal_error"},
        {"action": "account.open", "outcome": "succeeded", "reason": None},
    ],
)
async def test_only_successful_postings_reference_a_transaction(
    db_session: AsyncSession, values: dict[str, Any]
) -> None:
    user = await create_user(db_session)
    transaction_id = await _posted_transaction_id(db_session)

    async with raises_violation("ck_audit_events_transaction_iff_posted"):
        await _insert(db_session, actor_user_id=user.id, transaction_id=transaction_id, **values)


async def test_success_requires_an_actor(db_session: AsyncSession) -> None:
    async with raises_violation("ck_audit_events_actor_for_success"):
        await _insert(db_session, outcome="succeeded", reason=None)


# --- keys --------------------------------------------------------------------------------------


async def test_a_transaction_is_audited_as_succeeded_at_most_once(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    transaction_id = await _posted_transaction_id(db_session)
    success = {
        "action": "posting.transfer",
        "outcome": "succeeded",
        "reason": None,
        "actor_user_id": user.id,
        "transaction_id": transaction_id,
    }
    await _insert(db_session, **success)

    async with raises_violation("uq_audit_events_transaction_id"):
        await _insert(db_session, **success)


async def test_event_cannot_reference_an_unknown_transaction(db_session: AsyncSession) -> None:
    user = await create_user(db_session)

    async with raises_violation("fk_audit_events_transaction_id_transactions"):
        await _insert(
            db_session,
            action="posting.transfer",
            outcome="succeeded",
            reason=None,
            actor_user_id=user.id,
            transaction_id=uuid.uuid7(),
        )


async def test_raw_event_cannot_reference_an_unknown_user(db_session: AsyncSession) -> None:
    # The constraint exists; insert_audit_event (below) is what keeps it from losing events.
    async with raises_violation("fk_audit_events_actor_user_id_users"):
        await _insert(db_session, actor_user_id=uuid.uuid7())


# --- actor resolution (insert_audit_event) -----------------------------------------------------


async def _only_event(session: AsyncSession, record: AuditRecord) -> AuditEvent:
    before = await session.scalar(select(func.max(AuditEvent.id))) or 0
    await insert_audit_event(session, record)
    events = (await session.scalars(select(AuditEvent).where(AuditEvent.id > before))).all()
    assert len(events) == 1
    return events[0]


async def test_known_actor_is_stored_as_is(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    record = AuditRecord(
        AuditAction.ACCOUNT_OPEN,
        AuditOutcome.REJECTED,
        actor_user_id=user.id,
        reason="account_already_exists",
        details={"currency": "GBP"},
    )

    event = await _only_event(db_session, record)

    assert event.actor_user_id == user.id
    assert event.details == {"currency": "GBP"}


async def test_unknown_actor_becomes_null_and_is_kept_in_details(db_session: AsyncSession) -> None:
    claimed = uuid.uuid7()
    record = AuditRecord(
        AuditAction.ACCOUNT_OPEN,
        AuditOutcome.REJECTED,
        actor_user_id=claimed,
        reason="user_not_found",
        details={"currency": "EUR"},
    )

    event = await _only_event(db_session, record)

    assert event.actor_user_id is None
    assert event.details == {"currency": "EUR", "requested_user_id": str(claimed)}


async def test_success_for_an_unknown_actor_is_refused(db_session: AsyncSession) -> None:
    # A success always has a real actor; resolving an unknown one to NULL must not hide a bug.
    record = AuditRecord(
        AuditAction.ACCOUNT_OPEN, AuditOutcome.SUCCEEDED, actor_user_id=uuid.uuid7()
    )

    async with raises_violation("ck_audit_events_actor_for_success"):
        await insert_audit_event(db_session, record)


# --- append-only -------------------------------------------------------------------------------


async def test_event_cannot_be_updated(db_session: AsyncSession) -> None:
    event_id = await _insert(db_session)

    async with raises_violation("audit_events_immutable"):
        await db_session.execute(
            update(AuditEvent).where(AuditEvent.id == event_id).values(reason="other")
        )


async def test_event_cannot_be_deleted(db_session: AsyncSession) -> None:
    event_id = await _insert(db_session)

    async with raises_violation("audit_events_immutable"):
        await db_session.execute(delete(AuditEvent).where(AuditEvent.id == event_id))


async def test_audit_events_cannot_be_truncated(db_session: AsyncSession) -> None:
    await _insert(db_session)

    async with raises_violation("audit_events_immutable"):
        await db_session.execute(text("TRUNCATE audit_events"))


async def test_a_superuser_can_bypass_the_append_only_triggers(db_session: AsyncSession) -> None:
    # Documents a limit rather than a feature (ADR 0004): triggers bind ordinary sessions only.
    # The application role is currently a superuser, so it is NOT protected from itself.
    event_id = await _insert(db_session)
    await bypass_triggers(db_session)

    await db_session.execute(delete(AuditEvent).where(AuditEvent.id == event_id))

    assert await db_session.get(AuditEvent, event_id) is None
