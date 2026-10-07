import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from ledger_api.data.base import Base, CreatedAt, MinorUnits, TextEnum, UUIDPrimaryKey
from ledger_api.domain.account import AccountKind


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("kind IN ('customer', 'system')", name="kind"),
        # Customer accounts have an owner; the settlement (system) account never does.
        CheckConstraint("(kind = 'customer') = (user_id IS NOT NULL)", name="owner_matches_kind"),
        # No overdrafts for customers. The settlement account mirrors customer money, so it is
        # negative by design. Last line of defense if a posting path forgets to lock (ADR 0005).
        CheckConstraint(
            "kind = 'system' OR balance_minor >= 0", name="customer_balance_nonnegative"
        ),
        # One account per user per currency (ADR 0003).
        UniqueConstraint("user_id", "ledger_id"),
        # Target of the composite foreign key from ledger_entries.
        UniqueConstraint("id", "ledger_id"),
        # UNIQUE (user_id, ledger_id) treats NULL owners as distinct, so this rule needs its own
        # partial index: exactly one settlement account per ledger.
        Index(
            "uq_accounts_one_system_account_per_ledger",
            "ledger_id",
            unique=True,
            postgresql_where=text("kind = 'system'"),
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    ledger_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledgers.id", ondelete="RESTRICT"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    kind: Mapped[AccountKind] = mapped_column(TextEnum(AccountKind))
    # Cached projection of SUM(ledger_entries.amount_minor), maintained by the posting service
    # under a row lock (ADR 0003, ADR 0005).
    balance_minor: Mapped[MinorUnits] = mapped_column(server_default=text("0"))
    created_at: Mapped[CreatedAt]
