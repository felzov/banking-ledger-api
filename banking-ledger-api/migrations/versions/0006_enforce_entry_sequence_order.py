"""Reject ledger entries numbered out of order (ADR 0012).

GENERATED ALWAYS does not make sequence_number tamper-proof: INSERT ... OVERRIDING SYSTEM VALUE
lets any role with INSERT choose the value. A balanced, sealed posting numbered below an
account's history passes every other check and silently rewrites that account's statement:
every later running balance changes, and reconciliation cannot tell (totals still match).

A BEFORE INSERT row trigger now requires, for every new entry:

- sequence_number > the account's current maximum (ck_ledger_entries_sequence_monotonic): an
  entry can only ever be appended to an account's history, never inserted into it;
- sequence_number <= the sequence's last issued value (ck_ledger_entries_sequence_issued): a
  value jumped far ahead would otherwise make every later, legitimate entry of the account
  look backdated, and block the account for good. The identity default is drawn before
  BEFORE triggers run, so a legitimate value is always <= the last issued one.

The trigger first locks the entry's account (FOR UPDATE), then reads the maximum in a new
statement, i.e. a fresh READ COMMITTED snapshot. The lock makes the check race-free: an insert
that did not lock waits for any in-flight posting on the account and then sees its entries.
The composite foreign key's check also waits for that posting, but it runs AFTER BEFORE
triggers: without the trigger's own lock, the maximum would be read too early (missing the
in-flight entry) and a backdated entry would commit once the wait ends. Verified by replacing
the function with a lock-free variant: the backdated insert was accepted.
The posting service already holds that lock (it locks every account before writing, ADR 0010),
so for it this is a re-acquisition by the same transaction: no wait, no new lock order, no new
deadlock. A path that skips the protocol locks in its own order and may deadlock; PostgreSQL
detects and aborts it, which is the correct outcome for such a path.

The maximum is an index-only backward scan of uq_ledger_entries_account_id_sequence_number.
Like every trigger, this binds ordinary sessions only: a superuser or the table owner can
disable it (ADR 0004).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-10 15:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ASSERT_ENTRY_SEQUENCE_ORDER = """
CREATE FUNCTION assert_entry_sequence_order() RETURNS trigger
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

CREATE_TRIGGER = """
CREATE TRIGGER ledger_entries_sequence_order
    BEFORE INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION assert_entry_sequence_order()
"""


def upgrade() -> None:
    op.execute(ASSERT_ENTRY_SEQUENCE_ORDER)
    op.execute(CREATE_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER ledger_entries_sequence_order ON ledger_entries")
    op.execute("DROP FUNCTION assert_entry_sequence_order()")
