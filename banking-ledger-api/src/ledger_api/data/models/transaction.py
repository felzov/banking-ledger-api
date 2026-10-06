import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ledger_api.data.base import Base, CreatedAt, MinorUnits, TextEnum, UUIDPrimaryKey
from ledger_api.domain.transaction import TransactionKind


class Transaction(Base):
    """A posted financial transaction. Append-only: a row exists only once posted (ADR 0004).

    The database rejects UPDATE and DELETE, and at COMMIT it requires at least two entries
    that sum to zero (triggers in migration 0001).
    """

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("kind IN ('deposit', 'withdrawal', 'transfer')", name="kind"),
        # Target of the composite foreign key from ledger_entries.
        UniqueConstraint("id", "ledger_id"),
    )

    id: Mapped[UUIDPrimaryKey]
    ledger_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledgers.id", ondelete="RESTRICT"))
    kind: Mapped[TransactionKind] = mapped_column(TextEnum(TransactionKind))
    created_at: Mapped[CreatedAt]


class LedgerEntry(Base):
    """One signed movement on one account. Append-only."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        # Both foreign keys include ledger_id, so an entry, its transaction and its account are
        # always in the same ledger: cross-currency postings cannot be represented.
        ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["transactions.id", "transactions.ledger_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["account_id", "ledger_id"],
            ["accounts.id", "accounts.ledger_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount_minor <> 0", name="amount_nonzero"),
        # An account appears at most once per transaction (this also rules out A -> A transfers).
        # Leading transaction_id doubles as the index for the balance trigger's SUM.
        UniqueConstraint("transaction_id", "account_id"),
        # Account statements and reconciliation, paginated by the time-ordered UUIDv7 id.
        Index("ix_ledger_entries_account_id_id", "account_id", "id"),
    )

    id: Mapped[UUIDPrimaryKey]
    transaction_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID]
    ledger_id: Mapped[uuid.UUID]
    amount_minor: Mapped[MinorUnits]
