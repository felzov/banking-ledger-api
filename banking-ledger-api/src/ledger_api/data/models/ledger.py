from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ledger_api.data.base import Base, CreatedAt, TextEnum, UUIDPrimaryKey
from ledger_api.domain.currency import Currency


class Ledger(Base):
    """One ledger per currency. Rows are created by migrations only."""

    __tablename__ = "ledgers"
    __table_args__ = (CheckConstraint("currency IN ('GBP', 'EUR')", name="currency_supported"),)

    id: Mapped[UUIDPrimaryKey]
    currency: Mapped[Currency] = mapped_column(TextEnum(Currency), unique=True)
    created_at: Mapped[CreatedAt]
