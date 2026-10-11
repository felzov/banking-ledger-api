"""Add password credentials, bearer-token sessions and authentication audit actions (ADR 0013).

- user_credentials: one Argon2id password hash per user who has registered a password. Keyed by
  user; no credentials are backfilled, so users from before this migration cannot log in. The
  row is updatable (a rehash or, later, a password change rewrites it). The CHECK accepts only
  the Argon2id encoding, so a plaintext or foreign-format value cannot be stored.
- auth_sessions: one row per issued bearer token. Only the token's SHA-256 digest is stored
  (exactly 32 bytes, unique: the per-request lookup). expires_at is set by the service from
  database time; the CHECK caps any session at 24 hours as a defence against a bug issuing
  long-lived tokens (the policy is 30 minutes). A session can only ever be revoked: a trigger
  allows exactly one change, revoked_at from NULL to a time, and refuses every other UPDATE.
  DELETE stays allowed (a future cleanup of expired sessions); sessions are not financial
  evidence.
- audit_events: the action CHECK gains 'auth.login' and 'auth.logout'. Every other audit rule
  (reason, actor for success, no transaction outside postings) applies to them unchanged.

Downgrade refuses while credentials exist (dropping them would lock every user out) or while
authentication audit events exist (the old CHECK would reject them, and audit evidence is never
discarded). Sessions are dropped with their table: that only logs everyone out.

Like 0001, literals are hard-coded: a migration is a frozen snapshot.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-11 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIONS_0004 = (
    "action IN ('user.register', 'account.open', "
    "'posting.deposit', 'posting.withdrawal', 'posting.transfer')"
)
ACTIONS_0012 = (
    "action IN ('user.register', 'account.open', "
    "'posting.deposit', 'posting.withdrawal', 'posting.transfer', "
    "'auth.login', 'auth.logout')"
)

FORBID_SESSION_CHANGES = """
CREATE FUNCTION public.forbid_auth_session_changes() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- The only permitted change: revoking a live session, once.
    IF OLD.revoked_at IS NULL
       AND NEW.revoked_at IS NOT NULL
       AND (NEW.id, NEW.user_id, NEW.token_hash, NEW.created_at, NEW.expires_at)
           IS NOT DISTINCT FROM
           (OLD.id, OLD.user_id, OLD.token_hash, OLD.created_at, OLD.expires_at)
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'auth_sessions rows can only be revoked, once'
        USING ERRCODE = 'restrict_violation', CONSTRAINT = 'auth_sessions_revoke_only';
END;
$$
"""

CREATE_SESSION_TRIGGER = """
CREATE TRIGGER auth_sessions_revoke_only
    BEFORE UPDATE ON public.auth_sessions
    FOR EACH ROW EXECUTE FUNCTION public.forbid_auth_session_changes()
"""

REFUSE_IF_POPULATED = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.user_credentials) THEN
        RAISE EXCEPTION 'refusing to drop user_credentials: users would lose their passwords'
            USING HINT = 'Roll forward instead of downgrading past this revision.',
                  ERRCODE = 'dependent_objects_still_exist';
    END IF;
    IF EXISTS (SELECT 1 FROM public.audit_events WHERE action IN ('auth.login', 'auth.logout'))
    THEN
        RAISE EXCEPTION 'refusing to downgrade: authentication audit events exist'
            USING HINT = 'Roll forward instead of downgrading past this revision.',
                  ERRCODE = 'dependent_objects_still_exist';
    END IF;
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "user_credentials",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_credentials")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_credentials_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "password_hash LIKE '$argon2id$%' AND char_length(password_hash) <= 512",
            name=op.f("ck_user_credentials_password_hash_argon2id"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name=op.f("ck_user_credentials_updated_after_created")
        ),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_sessions_token_hash")),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name=op.f("ck_auth_sessions_token_hash_length")
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '24 hours'",
            name=op.f("ck_auth_sessions_expiry_window"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name=op.f("ck_auth_sessions_revoked_after_created"),
        ),
    )
    # A user's sessions (the foreign key's referencing side, and a future "revoke all").
    op.create_index(op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"])

    op.execute(FORBID_SESSION_CHANGES)
    op.execute(CREATE_SESSION_TRIGGER)

    op.drop_constraint(op.f("ck_audit_events_action"), "audit_events", type_="check")
    op.create_check_constraint(op.f("ck_audit_events_action"), "audit_events", ACTIONS_0012)


def downgrade() -> None:
    op.execute(REFUSE_IF_POPULATED)
    op.drop_constraint(op.f("ck_audit_events_action"), "audit_events", type_="check")
    op.create_check_constraint(op.f("ck_audit_events_action"), "audit_events", ACTIONS_0004)
    # Dropping the table drops its trigger and index; the trigger function goes last.
    op.drop_table("auth_sessions")
    op.execute("DROP FUNCTION public.forbid_auth_session_changes()")
    op.drop_table("user_credentials")
