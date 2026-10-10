"""Pin the deferred double-entry check's search_path against temporary tables (ADR 0012).

assert_transaction_balanced() (0001, sealed in 0002) runs at COMMIT for every new transaction
and every new ledger entry. It named ledger_entries and transactions without a schema and ran
with the caller's search_path. For unqualified relation names PostgreSQL searches the
session's temporary schema FIRST unless pg_temp is listed explicitly, and every role may
create temporary tables (TEMP is granted to PUBLIC). A session could therefore create its own
"ledger_entries" and "transactions" holding whatever rows it liked: the check then counted,
summed and compared those, and an unbalanced, single-entry or mis-sealed transaction could
commit. Same flaw, and same fix, as the ordering trigger in 0008.

The fix, with the logic otherwise unchanged:

- SET search_path = pg_catalog, pg_temp on the function: built-ins resolve from pg_catalog
  first, pg_temp is searched last, and public not at all;
- every application table is schema-qualified (public.ledger_entries, public.transactions).

The function stays SECURITY INVOKER. Both constraint triggers (transactions_balanced,
ledger_entries_balanced) call it by OID, so replacing it in place needs no trigger change.

Downgrade restores 0002's definition verbatim; CREATE OR REPLACE rewrites every attribute, so
the pinned search_path goes with it.

Like 0001, literals are hard-coded: a migration is a frozen snapshot.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-10 23:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ASSERT_TRANSACTION_BALANCED_0009 = """
CREATE OR REPLACE FUNCTION public.assert_transaction_balanced() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
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
      FROM public.ledger_entries
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

    SELECT entry_count INTO v_declared FROM public.transactions WHERE id = v_transaction_id;

    IF v_entry_count <> v_declared THEN
        RAISE EXCEPTION 'transaction % has % ledger entries but declares %',
            v_transaction_id, v_entry_count, v_declared
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_transactions_entry_count';
    END IF;

    RETURN NULL;
END;
$$
"""

# Verbatim from 0002 (ASSERT_TRANSACTION_BALANCED_SEALED), restored on downgrade.
ASSERT_TRANSACTION_BALANCED_0002 = """
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


def upgrade() -> None:
    op.execute(ASSERT_TRANSACTION_BALANCED_0009)


def downgrade() -> None:
    op.execute(ASSERT_TRANSACTION_BALANCED_0002)
