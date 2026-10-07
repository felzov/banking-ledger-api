from sqlalchemy import CheckConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from ledger_api.data.base import Base, CreatedAt, UUIDPrimaryKey


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Emails are normalised by the application; the database rejects any path that forgets,
        # which makes the plain UNIQUE constraint effectively case-insensitive.
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("char_length(email) BETWEEN 3 AND 320", name="email_length"),
    )

    id: Mapped[UUIDPrimaryKey]
    email: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[CreatedAt]
