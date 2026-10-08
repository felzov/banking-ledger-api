"""Seal every transaction with a declared entry count.

Closes the Phase 2 gap where balanced entries could be appended to an already-posted
transaction in a later database transaction: each sum-to-zero check passes, yet a historical
record changes. Every transaction now declares how many entries it has (entry_count, part of
the immutable header), and the deferred COMMIT-time check additionally requires the actual
number of entries to equal it. Any later append makes the count exceed the declaration.

The check runs after the existing two (>= 2 entries, sum = 0), so they keep reporting first.

Like 0001, literals are hard-coded: a migration is a frozen snapshot and imports nothing
from the application or from other migrations.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08 14:51:01.399549
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Existing rows (none are expected: no posting code existed before this revision) get their
# actual count. transactions is append-only, so the backfill bypasses the immutability
# trigger for this one statement, inside the migration's own transaction. (Separate
# statements: asyncpg executes one command per prepared statement.)
BACKFILL_ENTRY_COUNT = [
    "ALTER TABLE transactions DISABLE TRIGGER transactions_immutable",
    """
    UPDATE transactions t
       SET entry_count = (SELECT count(*) FROM ledger_entries e WHERE e.transaction_id = t.id)
    """,
    "ALTER TABLE transactions ENABLE TRIGGER transactions_immutable",
]

ASSERT_TRANSACTION_BALANCED_SEALED = """
CREATE OR REPLACE FUNCTION assert_transaction_balanced() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_transaction_id uuid;
    v_entry_count    bigint;
    v_total          numeric;  -- SUM(bigint) is numeric, so the sum itself cannot overflow
    v_declared       smallint;
BEGIN
    -- PL/pgSQL plans each statement on first execution, so the branch that is not taken
    -- never references a column the triggering table lacks.
    IF TG_TABLE_NAME = 'transactions' THEN
        v_transaction_id := NEW.id;
    ELSE
        v_transaction_id := NEW.transaction_id;
    END IF;

    SELECT count(*), coalesce(sum(amount_minor), 0)
      INTO v_entry_count, v_total
      FROM ledger_entries
     WHERE transaction_id = v_transaction_id;

    IF v_entry_count < 2 THEN
        RAISE EXCEPTION 'transaction % has % ledger entries; at least 2 are required',
            v_transaction_id, v_entry_count
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_transactions_min_two_entries';
    END IF;

    IF v_total <> 0 THEN
        RAISE EXCEPTION 'transaction % is unbalanced: its entries sum to %',
            v_transaction_id, v_total
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_transactions_balanced';
    END IF;

    SELECT entry_count INTO v_declared FROM transactions WHERE id = v_transaction_id;

    IF v_entry_count <> v_declared THEN
        RAISE EXCEPTION 'transaction % has % ledger entries but declares %',
            v_transaction_id, v_entry_count, v_declared
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_transactions_entry_count';
    END IF;

    RETURN NULL;
END;
$$
"""

# Verbatim from 0001, restored on downgrade.
ASSERT_TRANSACTION_BALANCED_0001 = """
CREATE OR REPLACE FUNCTION assert_transaction_balanced() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_transaction_id uuid;
    v_entry_count    bigint;
    v_total          numeric;  -- SUM(bigint) is numeric, so the sum itself cannot overflow
BEGIN
    -- PL/pgSQL plans each statement on first execution, so the branch that is not taken
    -- never references a column the triggering table lacks.
    IF TG_TABLE_NAME = 'transactions' THEN
        v_transaction_id := NEW.id;
    ELSE
        v_transaction_id := NEW.transaction_id;
    END IF;

    SELECT count(*), coalesce(sum(amount_minor), 0)
      INTO v_entry_count, v_total
      FROM ledger_entries
     WHERE transaction_id = v_transaction_id;

    IF v_entry_count < 2 THEN
        RAISE EXCEPTION 'transaction % has % ledger entries; at least 2 are required',
            v_transaction_id, v_entry_count
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_transactions_min_two_entries';
    END IF;

    IF v_total <> 0 THEN
        RAISE EXCEPTION 'transaction % is unbalanced: its entries sum to %',
            v_transaction_id, v_total
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_transactions_balanced';
    END IF;

    RETURN NULL;
END;
$$
"""


def upgrade() -> None:
    op.add_column("transactions", sa.Column("entry_count", sa.SmallInteger(), nullable=True))
    for statement in BACKFILL_ENTRY_COUNT:
        op.execute(statement)
    op.alter_column("transactions", "entry_count", nullable=False)
    op.create_check_constraint(
        op.f("ck_transactions_entry_count_min"), "transactions", "entry_count >= 2"
    )
    op.execute(ASSERT_TRANSACTION_BALANCED_SEALED)


def downgrade() -> None:
    op.execute(ASSERT_TRANSACTION_BALANCED_0001)
    op.drop_constraint(op.f("ck_transactions_entry_count_min"), "transactions", type_="check")
    op.drop_column("transactions", "entry_count")
