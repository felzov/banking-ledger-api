import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    SmallInteger,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ledger_api.data.base import Base, CreatedAt, MinorUnits, TextEnum, UUIDPrimaryKey
from ledger_api.domain.transaction import TransactionKind


class Transaction(Base):
    """A posted financial transaction. Append-only: a row exists only once posted (ADR 0004).

    The database rejects UPDATE and DELETE, and at COMMIT it requires at least two entries
    that sum to zero and whose number equals entry_count (triggers, migrations 0001 and 0002).
    """

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("kind IN ('deposit', 'withdrawal', 'transfer')", name="kind"),
        CheckConstraint("entry_count >= 2", name="entry_count_min"),
        # Target of the composite foreign key from ledger_entries.
        UniqueConstraint("id", "ledger_id"),
    )

    id: Mapped[UUIDPrimaryKey]
    ledger_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledgers.id", ondelete="RESTRICT"))
    kind: Mapped[TransactionKind] = mapped_column(TextEnum(TransactionKind))
    # Seals the transaction: the number of entries it was posted with. The header is immutable,
    # so entries appended later make the actual count exceed it and COMMIT fails.
    entry_count: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[CreatedAt]


class LedgerEntry(Base):
    """One signed movement on one account. Append-only.

    sequence_number orders an account's entries in commit order (ADR 0012, migration 0005).
    """

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
        # Account statements, keyset pagination and reconciliation. Unique: an account's
        # history is a total order.
        UniqueConstraint("account_id", "sequence_number"),
    )

    id: Mapped[UUIDPrimaryKey]
    transaction_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID]
    ledger_id: Mapped[uuid.UUID]
    amount_minor: Mapped[MinorUnits]
    # Drawn while the posting holds the account's row lock, which it keeps until COMMIT: for
    # one account, sequence order is commit order (not across accounts). CACHE 1 keeps values
    # strictly increasing across sessions. Gaps are normal. Never exposed outside the service.
    sequence_number: Mapped[int] = mapped_column(BigInteger, Identity(always=True, cache=1))
