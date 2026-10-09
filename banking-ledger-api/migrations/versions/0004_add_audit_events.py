"""Add the append-only audit_events table (ADR 0011).

One row per audited attempt: user registration, account opening and every posting, with its
outcome (succeeded, rejected, failed). Successful events commit in the business transaction;
rejected and failed ones are written after it rolls back.

The table is append-only like the ledger: BEFORE UPDATE OR DELETE (row) and BEFORE TRUNCATE
(statement) triggers raise for every role that does not disable them. A superuser or the table
owner can; the application currently connects as one (ADR 0004).

Downgrade refuses to drop a non-empty table: dropping it would destroy audit evidence. On a
database that holds events, roll forward instead. An empty database still round-trips.

Like 0001, literals are hard-coded: a migration is a frozen snapshot.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POSTING_ACTIONS = "('posting.deposit', 'posting.withdrawal', 'posting.transfer')"

FORBID_AUDIT_MUTATION = """
CREATE FUNCTION forbid_audit_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on audit_events is not allowed: audit events are append-only', TG_OP
        USING ERRCODE = 'restrict_violation', CONSTRAINT = 'audit_events_immutable';
END;
$$
"""

CREATE_TRIGGERS = [
    """
    CREATE TRIGGER audit_events_immutable
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation()
    """,
    """
    CREATE TRIGGER audit_events_no_truncate
        BEFORE TRUNCATE ON audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_audit_mutation()
    """,
]

# Audit evidence is not dropped by a routine downgrade (ADR 0011).
REFUSE_IF_NOT_EMPTY = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM audit_events) THEN
        RAISE EXCEPTION 'refusing to drop audit_events: it holds audit evidence'
            USING HINT = 'Roll forward instead of downgrading past this revision.',
                  ERRCODE = 'dependent_objects_still_exist';
    END IF;
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_events_actor_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_audit_events_transaction_id_transactions"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("transaction_id", name=op.f("uq_audit_events_transaction_id")),
        sa.CheckConstraint(
            "action IN ('user.register', 'account.open', "
            "'posting.deposit', 'posting.withdrawal', 'posting.transfer')",
            name=op.f("ck_audit_events_action"),
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'rejected', 'failed')", name=op.f("ck_audit_events_outcome")
        ),
        sa.CheckConstraint(
            "(outcome = 'succeeded') = (reason IS NULL)",
            name=op.f("ck_audit_events_reason_iff_unsuccessful"),
        ),
        sa.CheckConstraint(
            "reason ~ '^[a-z][a-z0-9_]{0,63}$'", name=op.f("ck_audit_events_reason_format")
        ),
        sa.CheckConstraint(
            "(transaction_id IS NOT NULL) = "
            f"(outcome = 'succeeded' AND action IN {POSTING_ACTIONS})",
            name=op.f("ck_audit_events_transaction_iff_posted"),
        ),
        sa.CheckConstraint(
            "outcome <> 'succeeded' OR actor_user_id IS NOT NULL",
            name=op.f("ck_audit_events_actor_for_success"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(details) = 'object'", name=op.f("ck_audit_events_details_object")
        ),
        sa.CheckConstraint(
            "octet_length(details::text) <= 2048", name=op.f("ck_audit_events_details_size")
        ),
        sa.CheckConstraint(
            "char_length(request_id) BETWEEN 1 AND 128",
            name=op.f("ck_audit_events_request_id_length"),
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 255",
            name=op.f("ck_audit_events_idempotency_key_length"),
        ),
    )
    op.create_index(
        op.f("ix_audit_events_actor_user_id_id"), "audit_events", ["actor_user_id", "id"]
    )

    op.execute(FORBID_AUDIT_MUTATION)
    for statement in CREATE_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.execute(REFUSE_IF_NOT_EMPTY)
    # Dropping the table drops its index and triggers; the trigger function goes last.
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION forbid_audit_mutation()")
