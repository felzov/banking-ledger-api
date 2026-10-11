import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ledger_api.data.base import Base, CreatedAt, MinorUnits, TextEnum, UUIDPrimaryKey
from ledger_api.domain.transaction import TransactionKind


class Transaction(Base):
    """A posted financial transaction. Append-only: a row exists only once posted (ADR 0004).

    The database rejects UPDATE and DELETE, and at COMMIT it requires at least two entries
    that sum to zero and whose number equals entry_count (triggers, migrations 0001 and 0002).

    A keyed posting names its idempotency key here, and the key names this transaction: at
    COMMIT both must reference each other, and the key's user must own the posting's own
    account (migration 0011). Rows are immutable, so an existing transaction can never be
    attached to a new key.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("kind IN ('deposit', 'withdrawal', 'transfer')", name="kind"),
        CheckConstraint("entry_count >= 2", name="entry_count_min"),
        # Target of the composite foreign key from ledger_entries.
        UniqueConstraint("id", "ledger_id"),
        # Unkeyed (both NULL, including every row from before 0011) or keyed (both set).
        CheckConstraint(
            "(idempotency_user_id IS NULL) = (idempotency_key IS NULL)",
            name="idempotency_key_pair",
        ),
        # Explicit names: the conventional ones would exceed PostgreSQL's 63 characters.
        UniqueConstraint(
            "idempotency_user_id", "idempotency_key", name="uq_transactions_idempotency_key"
        ),
        # Checked at COMMIT, like the key's own reference to this transaction. The two tables
        # reference each other; use_alter breaks the cycle for table ordering (the constraint is
        # added by ALTER TABLE, as migration 0011 does).
        ForeignKeyConstraint(
            ["idempotency_user_id", "idempotency_key"],
            ["idempotency_keys.user_id", "idempotency_keys.key"],
            name="fk_transactions_idempotency_keys",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    ledger_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledgers.id", ondelete="RESTRICT"))
    kind: Mapped[TransactionKind] = mapped_column(TextEnum(TransactionKind))
    # Seals the transaction: the number of entries it was posted with. The header is immutable,
    # so entries appended later make the actual count exceed it and COMMIT fails.
    entry_count: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[CreatedAt]
    # The idempotency key this transaction was posted under (ADR 0006), or NULL for both.
    idempotency_user_id: Mapped[uuid.UUID | None]
    idempotency_key: Mapped[str | None] = mapped_column(Text)


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
    # GENERATED ALWAYS can be overridden (OVERRIDING SYSTEM VALUE); a trigger (migration 0006)
    # rejects any number not after the account's latest or never issued by the sequence. The
    # trigger runs as the inserting role; only the sequence read is elevated, through a narrow
    # SECURITY DEFINER helper (migration 0007), so a role needs EXECUTE on that helper and no
    # privilege on the sequence itself.
    sequence_number: Mapped[int] = mapped_column(BigInteger, Identity(always=True, cache=1))
