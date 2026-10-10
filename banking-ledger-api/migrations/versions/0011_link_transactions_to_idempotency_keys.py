"""Link each keyed transaction to its idempotency key, in both directions (ADR 0006).

Until 0010 only the key pointed at its transaction (idempotency_keys.transaction_id), and the
foreign key proved only that SOME transaction existed. A database client with INSERT on
idempotency_keys could attach a key to an unrelated, older transaction, or to another user's:
a later legitimate request with that key would then "replay" it instead of posting.

The fix makes the immutable transaction row name its key:

- transactions.idempotency_user_id / idempotency_key: both NULL (unkeyed, and every row from
  before this migration) or both set (ck_transactions_idempotency_key_pair).
- uq_transactions_idempotency_key: at most one transaction per key.
- fk_transactions_idempotency_keys: the named key exists, checked at COMMIT.
- assert_idempotency_link(), run at COMMIT by a constraint trigger on each side (a new key; a
  new transaction that names a key): the key must reference exactly this transaction, the
  transaction must name exactly this key, and the key's user must own the posting's own
  customer account (credited by a deposit, debited by a withdrawal or a transfer).

Why this is sound: transactions are append-only (no UPDATE, trigger and privileges), so an
existing transaction can never come to name a new key, and a new transaction cannot name an
existing key, because that key must point back at it. Neither xmin nor timestamps are involved.

The function is SECURITY INVOKER (it reads only what a posting role can already read) with a
pinned search_path and schema-qualified names, so temporary tables cannot shadow what it reads
(as in 0008 and 0009).

Downgrade refuses while any transaction names a key: dropping the columns would discard the
link that proves which key a transaction was posted under. On such a database, roll forward.

Like 0001, literals are hard-coded: a migration is a frozen snapshot.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-11 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ASSERT_IDEMPOTENCY_LINK = """
CREATE FUNCTION public.assert_idempotency_link() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_user_id        uuid;
    v_key            text;
    v_transaction_id uuid;
BEGIN
    -- PL/pgSQL plans each statement on first execution, so the branch that is not taken never
    -- references a column the triggering table lacks.
    IF TG_TABLE_NAME = 'idempotency_keys' THEN
        v_user_id := NEW.user_id;
        v_key := NEW.key;
        v_transaction_id := NEW.transaction_id;
    ELSE
        v_user_id := NEW.idempotency_user_id;
        v_key := NEW.idempotency_key;
        v_transaction_id := NEW.id;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM public.idempotency_keys k
          JOIN public.transactions t ON t.id = k.transaction_id
         WHERE k.user_id = v_user_id
           AND k.key = v_key
           AND t.id = v_transaction_id
           AND t.idempotency_user_id = v_user_id
           AND t.idempotency_key = v_key
    ) THEN
        RAISE EXCEPTION 'idempotency key and transaction % do not reference each other',
            v_transaction_id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ck_idempotency_keys_transaction_link';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM public.transactions t
          JOIN public.ledger_entries e ON e.transaction_id = t.id
          JOIN public.accounts a ON a.id = e.account_id
         WHERE t.id = v_transaction_id
           AND a.kind = 'customer'
           AND a.user_id = v_user_id
           AND ((t.kind = 'deposit' AND e.amount_minor > 0)
                OR (t.kind IN ('withdrawal', 'transfer') AND e.amount_minor < 0))
    ) THEN
        RAISE EXCEPTION 'transaction % is not a posting of the idempotency key''s user',
            v_transaction_id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ck_idempotency_keys_transaction_owner';
    END IF;

    RETURN NULL;
END;
$$
"""

CREATE_TRIGGERS = [
    """
    CREATE CONSTRAINT TRIGGER idempotency_keys_linked
        AFTER INSERT ON public.idempotency_keys
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.assert_idempotency_link()
    """,
    """
    CREATE CONSTRAINT TRIGGER transactions_idempotency_linked
        AFTER INSERT ON public.transactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.idempotency_key IS NOT NULL)
        EXECUTE FUNCTION public.assert_idempotency_link()
    """,
]

REFUSE_IF_LINKED = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.transactions WHERE idempotency_key IS NOT NULL) THEN
        RAISE EXCEPTION 'refusing to drop the idempotency links of posted transactions'
            USING HINT = 'Roll forward instead of downgrading past this revision.',
                  ERRCODE = 'dependent_objects_still_exist';
    END IF;
END;
$$
"""


def upgrade() -> None:
    # Nullable, no default: adding them rewrites no row and fires no row trigger, so every
    # existing (append-only) transaction stays exactly as it was, unkeyed.
    op.add_column("transactions", sa.Column("idempotency_user_id", sa.Uuid(), nullable=True))
    op.add_column("transactions", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_transactions_idempotency_key_pair"),
        "transactions",
        "(idempotency_user_id IS NULL) = (idempotency_key IS NULL)",
    )
    op.create_unique_constraint(
        "uq_transactions_idempotency_key",
        "transactions",
        ["idempotency_user_id", "idempotency_key"],
    )
    op.create_foreign_key(
        "fk_transactions_idempotency_keys",
        "transactions",
        "idempotency_keys",
        ["idempotency_user_id", "idempotency_key"],
        ["user_id", "key"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(ASSERT_IDEMPOTENCY_LINK)
    for statement in CREATE_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.execute(REFUSE_IF_LINKED)
    op.execute("DROP TRIGGER transactions_idempotency_linked ON public.transactions")
    op.execute("DROP TRIGGER idempotency_keys_linked ON public.idempotency_keys")
    op.execute("DROP FUNCTION public.assert_idempotency_link()")
    op.drop_constraint("fk_transactions_idempotency_keys", "transactions", type_="foreignkey")
    op.drop_constraint("uq_transactions_idempotency_key", "transactions", type_="unique")
    op.drop_constraint(op.f("ck_transactions_idempotency_key_pair"), "transactions", type_="check")
    op.drop_column("transactions", "idempotency_key")
    op.drop_column("transactions", "idempotency_user_id")
