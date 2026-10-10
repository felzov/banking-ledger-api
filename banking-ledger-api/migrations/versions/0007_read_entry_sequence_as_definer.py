"""Read the last issued entry number through a narrow SECURITY DEFINER helper (ADR 0012).

0006's ordering trigger runs with the inserting role's privileges and called
pg_sequence_last_value() directly. Inserting into an identity column needs no privilege on its
sequence, but reading the sequence does: for a role without SELECT or USAGE on it,
pg_sequence_last_value() returns NULL (no error), and the trigger then rejected EVERY entry,
legitimate ones included, as "never issued". Invisible while the application connects as a
superuser; fatal for the least-privilege application role planned for Phase 10.

The fix elevates exactly one read, nothing else:

- public.ledger_entries_last_issued_sequence_number() is SECURITY DEFINER, owned by the owner
  of public.ledger_entries (who owns its identity sequence), takes no arguments and names its
  one sequence itself: it cannot be pointed at any other sequence. search_path is pinned to
  pg_catalog, pg_temp (pg_temp last), and every object is schema-qualified, so a caller cannot
  substitute objects. SECURITY DEFINER functions are never inlined.
- EXECUTE is revoked from PUBLIC: the value reveals global entry volume. The owner keeps it
  (the current application role is the owner and a superuser). The Phase 10 application role
  must be granted EXECUTE together with its table privileges; without it, inserts fail loudly
  with 42501 (permission denied for function ...), never with a misleading check violation.
- The trigger function itself stays SECURITY INVOKER: its account lock (FOR UPDATE) and its
  maximum query still run as, and in the transaction of, the inserting role. Only the line
  that reads the last issued value changes; every guarantee of 0006 is kept.

Downgrade restores 0006's trigger function verbatim, then drops the helper.

Like 0001, literals are hard-coded: a migration is a frozen snapshot.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-10 18:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HELPER = "public.ledger_entries_last_issued_sequence_number()"

CREATE_HELPER = f"""
CREATE FUNCTION {HELPER} RETURNS bigint
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT pg_catalog.pg_sequence_last_value(
        pg_catalog.pg_get_serial_sequence('public.ledger_entries', 'sequence_number')
            ::pg_catalog.regclass
    )
$$
"""

# Explicit ownership: the definer must be the owner of the table (and so of its sequence),
# whichever role happens to run the migration.
OWN_HELPER_AS_TABLE_OWNER = """
DO $$
BEGIN
    EXECUTE pg_catalog.format(
        'ALTER FUNCTION public.ledger_entries_last_issued_sequence_number() OWNER TO %I',
        (SELECT tableowner FROM pg_catalog.pg_tables
          WHERE schemaname = 'public' AND tablename = 'ledger_entries')
    );
END;
$$
"""

REVOKE_HELPER_FROM_PUBLIC = f"REVOKE ALL ON FUNCTION {HELPER} FROM PUBLIC"

# 0006's function with one change: the last issued value comes from the helper.
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

# Verbatim from 0006 (as OR REPLACE), restored on downgrade.
ASSERT_ENTRY_SEQUENCE_ORDER_0006 = """
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

    v_issued := pg_sequence_last_value(
        pg_get_serial_sequence('ledger_entries', 'sequence_number')::regclass
    );
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
    op.execute(CREATE_HELPER)
    op.execute(OWN_HELPER_AS_TABLE_OWNER)
    op.execute(REVOKE_HELPER_FROM_PUBLIC)
    op.execute(ASSERT_ENTRY_SEQUENCE_ORDER_0007)


def downgrade() -> None:
    # The trigger function stops referencing the helper first; then the helper can go.
    op.execute(ASSERT_ENTRY_SEQUENCE_ORDER_0006)
    op.execute(f"DROP FUNCTION {HELPER}")
