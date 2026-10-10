"""Pin the ordering trigger's search_path so temporary tables cannot shadow it (ADR 0012).

0006/0007's assert_entry_sequence_order() named accounts and ledger_entries without a schema
and ran with the caller's search_path. For unqualified relation names PostgreSQL searches the
session's temporary schema FIRST unless pg_temp is listed explicitly, and every role may
create temporary tables by default (TEMP is granted to PUBLIC). A session could therefore
create its own empty "accounts" and "ledger_entries": the trigger then locked and read those,
saw no history for the account, and accepted an entry numbered before the account's latest
(OVERRIDING SYSTEM VALUE). Irrelevant while the application connects as a superuser (it
bypasses triggers anyway), but it voided the guarantee 0007 gives a least-privilege role.

The fix, with the logic otherwise unchanged:

- SET search_path = pg_catalog, pg_temp on the function: built-ins resolve from pg_catalog
  first, pg_temp is searched last, and public not at all;
- every application object is schema-qualified (public.accounts, public.ledger_entries,
  public.ledger_entries_last_issued_sequence_number()).

The function stays SECURITY INVOKER: the pinned path changes where names resolve, not whose
privileges apply.

Downgrade restores 0007's definition verbatim; CREATE OR REPLACE rewrites every attribute, so
the pinned search_path goes with it.

Like 0001, literals are hard-coded: a migration is a frozen snapshot.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-10 21:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ASSERT_ENTRY_SEQUENCE_ORDER_0008 = """
CREATE OR REPLACE FUNCTION public.assert_entry_sequence_order() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_latest bigint;
    v_issued bigint;
BEGIN
    -- Serialize with every other writer of this account's history. Already held (a no-op)
    -- inside the posting protocol.
    PERFORM 1 FROM public.accounts WHERE id = NEW.account_id FOR UPDATE;

    SELECT max(sequence_number) INTO v_latest
      FROM public.ledger_entries
     WHERE account_id = NEW.account_id;

    IF NEW.sequence_number <= v_latest THEN
        RAISE EXCEPTION 'ledger entry sequence number % is not after % on account %',
            NEW.sequence_number, v_latest, NEW.account_id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ck_ledger_entries_sequence_monotonic';
    END IF;

    -- Read with the helper owner's privileges (migration 0007): the inserting role needs
    -- EXECUTE on the helper, not access to the sequence. NULL now only means "never used".
    v_issued := public.ledger_entries_last_issued_sequence_number();
    IF v_issued IS NULL OR NEW.sequence_number > v_issued THEN
        RAISE EXCEPTION 'ledger entry sequence number % was never issued (last issued: %)',
            NEW.sequence_number, v_issued
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ck_ledger_entries_sequence_issued';
    END IF;

    RETURN NEW;
END;
$$
"""

# Verbatim from 0007, restored on downgrade.
ASSERT_ENTRY_SEQUENCE_ORDER_0007 = """
CREATE OR REPLACE FUNCTION assert_entry_sequence_order() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_latest bigint;
    v_issued bigint;
BEGIN
    -- Serialize with every other writer of this account's history. Already held (a no-op)
    -- inside the posting protocol.
    PERFORM 1 FROM accounts WHERE id = NEW.account_id FOR UPDATE;

    SELECT max(sequence_number) INTO v_latest
      FROM ledger_entries
     WHERE account_id = NEW.account_id;

    IF NEW.sequence_number <= v_latest THEN
        RAISE EXCEPTION 'ledger entry sequence number % is not after % on account %',
            NEW.sequence_number, v_latest, NEW.account_id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ck_ledger_entries_sequence_monotonic';
    END IF;

    -- Read with the helper owner's privileges (migration 0007): the inserting role needs
    -- EXECUTE on the helper, not access to the sequence. NULL now only means "never used".
    v_issued := public.ledger_entries_last_issued_sequence_number();
    IF v_issued IS NULL OR NEW.sequence_number > v_issued THEN
        RAISE EXCEPTION 'ledger entry sequence number % was never issued (last issued: %)',
            NEW.sequence_number, v_issued
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ck_ledger_entries_sequence_issued';
    END IF;

    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    op.execute(ASSERT_ENTRY_SEQUENCE_ORDER_0008)


def downgrade() -> None:
    op.execute(ASSERT_ENTRY_SEQUENCE_ORDER_0007)
