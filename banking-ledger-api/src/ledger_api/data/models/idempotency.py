import uuid

from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ledger_api.data.base import Base, CreatedAt


class IdempotencyKey(Base):
    """An idempotency key consumed by a successful posting (ADR 0006). Append-only: the database
    rejects UPDATE, DELETE and TRUNCATE (triggers, migration 0010).

    Written as the first statement of the posting transaction, so it commits with the posting or
    not at all: rejected and failed postings never leave a key behind.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        # Visible ASCII, 1-255 characters (mirrors domain.idempotency.validate_idempotency_key).
        CheckConstraint(r"key ~ '^[\x21-\x7e]{1,255}$'", name="key_format"),
        # SHA-256.
        CheckConstraint("octet_length(request_fingerprint) = 32", name="fingerprint_length"),
        # One key per transaction.
        UniqueConstraint("transaction_id"),
    )

    # Keys are scoped per user; (user_id, key) is also the ON CONFLICT target of the claim.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary)
    # Generated before the transaction header is inserted, later in the same database
    # transaction: the foreign key is checked at COMMIT.
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT", deferrable=True, initially="DEFERRED")
    )
    created_at: Mapped[CreatedAt]
