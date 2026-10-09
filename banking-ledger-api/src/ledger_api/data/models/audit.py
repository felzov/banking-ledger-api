import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ledger_api.data.base import Base, TextEnum
from ledger_api.domain.audit import AuditAction, AuditOutcome


class AuditEvent(Base):
    """One audited attempt (ADR 0011). Append-only: the database rejects UPDATE, DELETE and
    TRUNCATE (triggers, migration 0004).

    The CHECK constraints mirror domain.audit.AuditRecord. The primary key is a BIGINT identity
    rather than the UUIDv7 used elsewhere: audit ids are never exposed or built before I/O.
    Identity values have gaps (a rolled-back success event consumes one), so a gap is normal and
    never evidence of a deleted event.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('user.register', 'account.open', "
            "'posting.deposit', 'posting.withdrawal', 'posting.transfer')",
            name="action",
        ),
        CheckConstraint("outcome IN ('succeeded', 'rejected', 'failed')", name="outcome"),
        CheckConstraint(
            "(outcome = 'succeeded') = (reason IS NULL)", name="reason_iff_unsuccessful"
        ),
        CheckConstraint("reason ~ '^[a-z][a-z0-9_]{0,63}$'", name="reason_format"),
        # A transaction is referenced exactly by successful postings.
        CheckConstraint(
            "(transaction_id IS NOT NULL) = (outcome = 'succeeded' AND action IN "
            "('posting.deposit', 'posting.withdrawal', 'posting.transfer'))",
            name="transaction_iff_posted",
        ),
        # Rejections may name an unknown user (stored as NULL); a success always has an actor.
        CheckConstraint(
            "outcome <> 'succeeded' OR actor_user_id IS NOT NULL", name="actor_for_success"
        ),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="details_object"),
        CheckConstraint("octet_length(details::text) <= 2048", name="details_size"),
        CheckConstraint("char_length(request_id) BETWEEN 1 AND 128", name="request_id_length"),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 255", name="idempotency_key_length"
        ),
        # A posted transaction is audited at most once (NULLs are distinct).
        UniqueConstraint("transaction_id"),
        # One user's audit trail, in insertion order.
        Index("ix_audit_events_actor_user_id_id", "actor_user_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    action: Mapped[AuditAction] = mapped_column(TextEnum(AuditAction))
    outcome: Mapped[AuditOutcome] = mapped_column(TextEnum(AuditOutcome))
    reason: Mapped[str | None] = mapped_column(Text)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT")
    )
    # Reserved: request correlation (Phase 10) and idempotency keys (Phase 6). NULL until then.
    request_id: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
