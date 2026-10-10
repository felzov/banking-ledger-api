"""user.register and account.open are audited (ADR 0011). Rolled-back test connection: the
services' transactions (and their separate audit transactions) are SAVEPOINTs on it."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.audit import insert_audit_event
from ledger_api.data.models import Account, AuditEvent
from ledger_api.domain.audit import AuditAction, AuditOutcome, AuditRecord
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import (
    AccountAlreadyExistsError,
    EmailAlreadyRegisteredError,
    UserNotFoundError,
)
from ledger_api.services import audit as audit_service
from ledger_api.services.accounts import open_customer_account
from ledger_api.services.users import register_user
from tests.conftest import SessionFactory


class InjectedAuditFailureError(Exception):
    pass


async def _latest_event_id(session: AsyncSession) -> int:
    return await session.scalar(select(func.max(AuditEvent.id))) or 0


async def _events_since(session: AsyncSession, event_id: int) -> list[AuditEvent]:
    statement = select(AuditEvent).where(AuditEvent.id > event_id).order_by(AuditEvent.id)
    return list((await session.scalars(statement)).all())


# --- user.register -----------------------------------------------------------------------------


async def test_registration_is_audited_without_the_email_address(
    db_session: AsyncSession, new_session: SessionFactory
) -> None:
    since = await _latest_event_id(db_session)

    user = await register_user(new_session(), email="audited@example.test")

    [event] = await _events_since(db_session, since)
    assert (event.action, event.outcome) == (AuditAction.USER_REGISTER, AuditOutcome.SUCCEEDED)
    assert event.actor_user_id == user.id
    assert event.details == {}  # personal data stays out of the audit trail


async def test_duplicate_registration_is_audited_as_rejected(
    db_session: AsyncSession, new_session: SessionFactory
) -> None:
    await register_user(new_session(), email="twice@example.test")
    since = await _latest_event_id(db_session)

    with pytest.raises(EmailAlreadyRegisteredError):
        await register_user(new_session(), email="twice@example.test")

    [event] = await _events_since(db_session, since)
    assert (event.outcome, event.reason) == (AuditOutcome.REJECTED, "email_already_registered")
    assert event.actor_user_id is None
    assert event.details == {}


# --- account.open ------------------------------------------------------------------------------


async def test_account_opening_is_audited_with_the_new_account(
    db_session: AsyncSession, new_session: SessionFactory
) -> None:
    user = await register_user(new_session(), email="opener@example.test")
    since = await _latest_event_id(db_session)

    account = await open_customer_account(new_session(), owner_id=user.id, currency=Currency.EUR)

    [event] = await _events_since(db_session, since)
    assert (event.action, event.outcome) == (AuditAction.ACCOUNT_OPEN, AuditOutcome.SUCCEEDED)
    assert event.actor_user_id == user.id
    assert event.details == {"currency": "EUR", "account_id": str(account.id)}


async def test_second_account_in_a_currency_is_audited_as_rejected(
    db_session: AsyncSession, new_session: SessionFactory
) -> None:
    user = await register_user(new_session(), email="greedy@example.test")
    await open_customer_account(new_session(), owner_id=user.id, currency=Currency.GBP)
    since = await _latest_event_id(db_session)

    with pytest.raises(AccountAlreadyExistsError):
        await open_customer_account(new_session(), owner_id=user.id, currency=Currency.GBP)

    [event] = await _events_since(db_session, since)
    assert (event.outcome, event.reason) == (AuditOutcome.REJECTED, "account_already_exists")
    assert event.actor_user_id == user.id
    assert event.details == {"currency": "GBP"}  # no account was opened


async def test_account_for_an_unknown_user_is_audited_without_an_actor(
    db_session: AsyncSession, new_session: SessionFactory
) -> None:
    stranger = uuid.uuid7()
    since = await _latest_event_id(db_session)

    with pytest.raises(UserNotFoundError):
        await open_customer_account(new_session(), owner_id=stranger, currency=Currency.GBP)

    [event] = await _events_since(db_session, since)
    assert (event.outcome, event.reason) == (AuditOutcome.REJECTED, "user_not_found")
    # No foreign-key violation: the unknown id is kept in details instead.
    assert event.actor_user_id is None
    assert event.details == {"currency": "GBP", "requested_user_id": str(stranger)}


async def test_failed_account_opening_never_names_the_rolled_back_account(
    db_session: AsyncSession, new_session: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The account row is flushed (it has an id), then the success audit write fails and the
    # whole transaction rolls back. The failed event must not name that account: it never
    # existed outside the rolled-back transaction.
    user = await register_user(new_session(), email="unlucky@example.test")
    injected = InjectedAuditFailureError()

    async def fail_success_events(session: AsyncSession, record: AuditRecord) -> None:
        if record.outcome == AuditOutcome.SUCCEEDED:
            raise injected
        await insert_audit_event(session, record)

    monkeypatch.setattr(audit_service, "insert_audit_event", fail_success_events)
    since = await _latest_event_id(db_session)

    with pytest.raises(InjectedAuditFailureError):
        await open_customer_account(new_session(), owner_id=user.id, currency=Currency.GBP)

    [event] = await _events_since(db_session, since)
    assert (event.outcome, event.reason) == (AuditOutcome.FAILED, "internal_error")
    assert "account_id" not in event.details
    assert event.details == {"currency": "GBP", "error_type": "InjectedAuditFailureError"}
    accounts = await db_session.scalar(
        select(func.count()).select_from(Account).where(Account.user_id == user.id)
    )
    assert accounts == 0


# --- not audited -------------------------------------------------------------------------------


async def test_reads_and_malformed_requests_are_not_audited(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await api_client.post("/users", json={"email": "reader@example.com"})
    user_id = response.json()["id"]
    since = await _latest_event_id(db_session)

    assert (await api_client.get(f"/users/{user_id}")).status_code == 200
    assert (await api_client.get(f"/users/{user_id}/accounts")).status_code == 200
    assert (await api_client.get("/health/live")).status_code == 200
    malformed = await api_client.post(f"/users/{user_id}/accounts", json={"currency": "XYZ"})
    assert malformed.status_code == 422

    assert await _events_since(db_session, since) == []


async def test_api_rejection_is_audited_and_still_returns_the_business_error(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    await api_client.post("/users", json={"email": "api-dup@example.com"})
    since = await _latest_event_id(db_session)

    response = await api_client.post("/users", json={"email": "api-dup@example.com"})

    assert response.status_code == 409
    assert response.json()["code"] == "email_already_registered"
    [event] = await _events_since(db_session, since)
    assert event.reason == "email_already_registered"
