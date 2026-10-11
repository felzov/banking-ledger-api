import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ledger_api.data.base import Base, CreatedAt, UUIDPrimaryKey


class UserCredential(Base):
    """A user's password, as an Argon2id hash only (ADR 0013, migration 0012).

    Optional per user: no credentials are backfilled, so a user without a row cannot log in.
    Updatable (a rehash or, later, a password change rewrites it).
    """

    __tablename__ = "user_credentials"
    __table_args__ = (
        # Only the Argon2id encoding (domain.passwords): never plaintext or another format.
        CheckConstraint(
            "password_hash LIKE '$argon2id$%' AND char_length(password_hash) <= 512",
            name="password_hash_argon2id",
        ),
        CheckConstraint("updated_at >= created_at", name="updated_after_created"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    """One issued bearer token (ADR 0013, migration 0012). Only its SHA-256 digest is stored.

    expires_at is set from database time by the service; a session lasts at most 24 hours
    (CHECK; the policy is 30 minutes). Rows can only be revoked, once: a trigger refuses any
    other UPDATE. DELETE is allowed (cleanup of expired sessions).
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash"),
        CheckConstraint("octet_length(token_hash) = 32", name="token_hash_length"),
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '24 hours'",
            name="expiry_window",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at", name="revoked_after_created"
        ),
    )

    id: Mapped[UUIDPrimaryKey]
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    # The SHA-256 digest of the token (domain.tokens.hash_token); never the token itself.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[CreatedAt]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
