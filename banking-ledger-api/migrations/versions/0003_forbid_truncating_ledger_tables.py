"""Forbid TRUNCATE on the append-only ledger tables.

The row-level immutability triggers of 0001 fire on UPDATE and DELETE only. TRUNCATE fires no
row triggers, and nothing references ledger_entries by foreign key, so a single
TRUNCATE ledger_entries used to erase the whole ledger history. Statement-level BEFORE TRUNCATE
triggers close that gap, reusing forbid_ledger_mutation() (its TG_OP is then 'TRUNCATE').

Like every trigger, these bind ordinary sessions only: a superuser or the table owner can still
disable or drop them (ADR 0004). They stop mistakes and application bugs, not a hostile owner.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09 10:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("transactions", "ledger_entries")


def upgrade() -> None:
    for table in TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_truncate
                BEFORE TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION forbid_ledger_mutation()
            """
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER {table}_no_truncate ON {table}")
