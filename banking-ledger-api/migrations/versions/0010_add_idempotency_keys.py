"""Add the append-only idempotency_keys table (ADR 0006, Phase 6 amendment).

One row per idempotency key a user has consumed with a SUCCESSFUL posting. The row is the
first write of the posting transaction (INSERT ... ON CONFLICT DO NOTHING): a concurrent
request with the same key waits on it, then replays or runs. Rejected and failed postings roll
the row back with everything else, so only committed postings ever own a key.

- PRIMARY KEY (user_id, key): keys are scoped per user, and this is the ON CONFLICT target.
- transaction_id: the posting the key produced. Its id is generated before the header exists,
  so the foreign key is DEFERRABLE INITIALLY DEFERRED and checked at COMMIT: a committed key
  always references a committed transaction. Unique: one key per transaction.
- request_fingerprint: SHA-256 (32 bytes) of the canonical business fields; the same key with
  a different fingerprint is refused (idempotency_key_mismatch).
- key: visible ASCII (0x21-0x7E), 1-255 characters. Written with hex escapes, not a character
  range, so the check never depends on a collation.

Keys are kept indefinitely (MVP). Rows are append-only: BEFORE UPDATE OR DELETE (row) and
BEFORE TRUNCATE (statement) triggers raise. A deleted key would let a retried request run a
second time. The trigger function reads no table, but its search_path is pinned like the other
trigger functions (0008, 0009).

Downgrade refuses to drop a non-empty table: dropping it would silently re-enable duplicate
execution of retried requests. On a database that holds keys, roll forward instead.

Like 0001, literals are hard-coded: a migration is a frozen snapshot.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-10 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FORBID_IDEMPOTENCY_KEY_MUTATION = """
CREATE FUNCTION public.forbid_idempotency_key_mutation() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION '% on idempotency_keys is not allowed: idempotency keys are append-only',
        TG_OP
        USING ERRCODE = 'restrict_violation', CONSTRAINT = 'idempotency_keys_immutable';
END;
$$
"""

CREATE_TRIGGERS = [
    """
    CREATE TRIGGER idempotency_keys_immutable
        BEFORE UPDATE OR DELETE ON idempotency_keys
        FOR EACH ROW EXECUTE FUNCTION public.forbid_idempotency_key_mutation()
    """,
    """
    CREATE TRIGGER idempotency_keys_no_truncate
        BEFORE TRUNCATE ON idempotency_keys
        FOR EACH STATEMENT EXECUTE FUNCTION public.forbid_idempotency_key_mutation()
    """,
]

REFUSE_IF_NOT_EMPTY = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM idempotency_keys) THEN
        RAISE EXCEPTION 'refusing to drop idempotency_keys: retried requests could run twice'
            USING HINT = 'Roll forward instead of downgrading past this revision.',
                  ERRCODE = 'dependent_objects_still_exist';
    END IF;
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "key", name=op.f("pk_idempotency_keys")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_idempotency_keys_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_idempotency_keys_transaction_id_transactions"),
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("transaction_id", name=op.f("uq_idempotency_keys_transaction_id")),
        sa.CheckConstraint(
            r"key ~ '^[\x21-\x7e]{1,255}$'", name=op.f("ck_idempotency_keys_key_format")
        ),
        sa.CheckConstraint(
            "octet_length(request_fingerprint) = 32",
            name=op.f("ck_idempotency_keys_fingerprint_length"),
        ),
    )

    op.execute(FORBID_IDEMPOTENCY_KEY_MUTATION)
    for statement in CREATE_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.execute(REFUSE_IF_NOT_EMPTY)
    # Dropping the table drops its triggers; the trigger function goes last.
    op.drop_table("idempotency_keys")
    op.execute("DROP FUNCTION public.forbid_idempotency_key_mutation()")
