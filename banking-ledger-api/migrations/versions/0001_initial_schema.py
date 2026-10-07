"""Initial schema: users, per-currency ledgers, accounts, transactions and ledger entries.

Besides tables and constraints, this migration installs the database-level ledger invariants
(ADR 0003, ADR 0004) and seeds the reference data the schema depends on.

Literals (currencies, kinds) are deliberately hard-coded instead of imported from the
application: a migration is a frozen snapshot of history and must not change meaning when
application code evolves.

Revision ID: 0001
Revises:
Create Date: 2026-10-06 12:15:03.963344
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Constraint names are wrapped in op.f(): they are already final. Without it, Alembic applies
# Base.metadata's naming convention again and "ck_users_x" becomes "ck_users_ck_users_x".
UUID_V7 = sa.text("uuidv7()")
NOW = sa.text("now()")

# ADR 0004: posted ledger records are append-only. Fires for every role, including the owner.
FORBID_LEDGER_MUTATION = """
CREATE FUNCTION forbid_ledger_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on % is not allowed: posted ledger records are immutable',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation', CONSTRAINT = TG_TABLE_NAME || '_immutable';
END;
$$
"""

# ADR 0003: every posted transaction has at least two entries that sum to zero. Rows of one
# posting are inserted one by one, so the check only makes sense once all of them exist: the
# triggers that call this function are deferred to COMMIT.
ASSERT_TRANSACTION_BALANCED = """
CREATE FUNCTION assert_transaction_balanced() RETURNS trigger
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

CREATE_TRIGGERS = [
    """
    CREATE TRIGGER transactions_immutable
        BEFORE UPDATE OR DELETE ON transactions
        FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation()
    """,
    """
    CREATE TRIGGER ledger_entries_immutable
        BEFORE UPDATE OR DELETE ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation()
    """,
    # Catches a transaction with zero entries: the ledger_entries trigger would never fire.
    """
    CREATE CONSTRAINT TRIGGER transactions_balanced
        AFTER INSERT ON transactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION assert_transaction_balanced()
    """,
    """
    CREATE CONSTRAINT TRIGGER ledger_entries_balanced
        AFTER INSERT ON ledger_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION assert_transaction_balanced()
    """,
]

# Reference data, not demo data: every ledger needs its settlement account (ADR 0003).
SEED_LEDGERS = "INSERT INTO ledgers (currency) VALUES ('GBP'), ('EUR')"
SEED_SETTLEMENT_ACCOUNTS = "INSERT INTO accounts (ledger_id, kind) SELECT id, 'system' FROM ledgers"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=UUID_V7, nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
        sa.CheckConstraint(
            "char_length(email) BETWEEN 3 AND 320", name=op.f("ck_users_email_length")
        ),
    )

    op.create_table(
        "ledgers",
        sa.Column("id", sa.Uuid(), server_default=UUID_V7, nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledgers")),
        sa.UniqueConstraint("currency", name=op.f("uq_ledgers_currency")),
        sa.CheckConstraint(
            "currency IN ('GBP', 'EUR')", name=op.f("ck_ledgers_currency_supported")
        ),
    )

    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), server_default=UUID_V7, nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("balance_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.ForeignKeyConstraint(
            ["ledger_id"],
            ["ledgers.id"],
            name=op.f("fk_accounts_ledger_id_ledgers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_accounts_user_id_users"), ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("user_id", "ledger_id", name=op.f("uq_accounts_user_id_ledger_id")),
        sa.UniqueConstraint("id", "ledger_id", name=op.f("uq_accounts_id_ledger_id")),
        sa.CheckConstraint("kind IN ('customer', 'system')", name=op.f("ck_accounts_kind")),
        sa.CheckConstraint(
            "(kind = 'customer') = (user_id IS NOT NULL)",
            name=op.f("ck_accounts_owner_matches_kind"),
        ),
        sa.CheckConstraint(
            "kind = 'system' OR balance_minor >= 0",
            name=op.f("ck_accounts_customer_balance_nonnegative"),
        ),
    )
    op.create_index(
        op.f("uq_accounts_one_system_account_per_ledger"),
        "accounts",
        ["ledger_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'system'"),
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), server_default=UUID_V7, nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.ForeignKeyConstraint(
            ["ledger_id"],
            ["ledgers.id"],
            name=op.f("fk_transactions_ledger_id_ledgers"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "ledger_id", name=op.f("uq_transactions_id_ledger_id")),
        sa.CheckConstraint(
            "kind IN ('deposit', 'withdrawal', 'transfer')", name=op.f("ck_transactions_kind")
        ),
    )

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Uuid(), server_default=UUID_V7, nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_entries")),
        # Composite keys pin the entry, its transaction and its account to the same ledger.
        sa.ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["transactions.id", "transactions.ledger_id"],
            name=op.f("fk_ledger_entries_transaction_id_ledger_id_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "ledger_id"],
            ["accounts.id", "accounts.ledger_id"],
            name=op.f("fk_ledger_entries_account_id_ledger_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "transaction_id", "account_id", name=op.f("uq_ledger_entries_transaction_id_account_id")
        ),
        sa.CheckConstraint("amount_minor <> 0", name=op.f("ck_ledger_entries_amount_nonzero")),
    )
    op.create_index(op.f("ix_ledger_entries_account_id_id"), "ledger_entries", ["account_id", "id"])

    op.execute(FORBID_LEDGER_MUTATION)
    op.execute(ASSERT_TRANSACTION_BALANCED)
    for statement in CREATE_TRIGGERS:
        op.execute(statement)

    op.execute(SEED_LEDGERS)
    op.execute(SEED_SETTLEMENT_ACCOUNTS)


def downgrade() -> None:
    # Dropping a table drops its indexes and triggers; the trigger functions go last.
    op.drop_table("ledger_entries")
    op.drop_table("transactions")
    op.drop_table("accounts")
    op.drop_table("ledgers")
    op.drop_table("users")
    op.execute("DROP FUNCTION assert_transaction_balanced()")
    op.execute("DROP FUNCTION forbid_ledger_mutation()")
