"""Audit event persistence. Callers own the transaction; nothing here commits."""

from typing import Any

from sqlalchemy import ColumnElement, case, exists, insert, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import AuditEvent, User
from ledger_api.domain.audit import REQUESTED_USER_ID, AuditRecord


async def insert_audit_event(session: AsyncSession, record: AuditRecord) -> None:
    """Insert one audit event.

    Until authentication exists, the claimed actor may not be a real user. It is resolved in
    the same statement: a known user is stored as actor_user_id; an unknown one becomes NULL
    (no foreign-key violation, so the event is never lost to it) and its id is kept in
    details.requested_user_id. Users are never deleted, so the lookup cannot race.
    """
    details = dict(record.details)
    actor_user_id = None
    stored_details: ColumnElement[Any] = literal(details, JSONB)
    if record.actor_user_id is not None:
        claimed = record.actor_user_id
        actor_user_id = select(User.id).where(User.id == claimed).scalar_subquery()
        stored_details = case(
            (exists().where(User.id == claimed), stored_details),
            else_=literal({**details, REQUESTED_USER_ID: str(claimed)}, JSONB),
        )
    await session.execute(
        insert(AuditEvent).values(
            action=record.action,
            outcome=record.outcome,
            actor_user_id=actor_user_id,
            reason=record.reason,
            transaction_id=record.transaction_id,
            details=stored_details,
        )
    )
