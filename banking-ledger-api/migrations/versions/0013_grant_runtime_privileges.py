"""Grant the application's runtime role exactly the privileges it uses (Phase 10, ADR 0015).

The role bootstrap (python -m ledger_api.admin.bootstrap) creates ledger_runtime, a NOLOGIN
group, and makes the API's login role (ledger_app) a member of it. Until now it held no privilege
on any table. This migration grants it, table by table and column by column, what the
application's queries need, and nothing else:

- SELECT where the application reads, including the reads its triggers do as the invoker
  (the deferred double-entry, entry-count and idempotency-link checks, the ordering trigger)
  and RETURNING clauses. Never on audit_events: the application writes audit events but never
  reads them (its insert has no RETURNING since this phase), nor on alembic_version.
- INSERT on the columns the application writes, never on those the database fills itself:
  created_at and occurred_at (no backdating), accounts.balance_minor (no account opened with
  money in it), auth_sessions.revoked_at (no session born revoked) and
  ledger_entries.sequence_number. Writing an identity column requires INSERT on it, so
  INSERT ... OVERRIDING SYSTEM VALUE (ADR 0012) is now refused by privilege, before the ordering
  trigger is even reached.
- UPDATE on the only columns the application changes: accounts.balance_minor (which also
  allows the FOR UPDATE row locks), user_credentials.password_hash and updated_at (a rehash),
  auth_sessions.revoked_at (logout).
- No DELETE, TRUNCATE, REFERENCES or TRIGGER anywhere (foreign keys are checked as the owner),
  no privilege on any sequence (inserting into an identity column needs none), no grant option.
- EXECUTE on the 0007 sequence helper only, which the ordering trigger calls as the invoker.
  Its owner, SECURITY DEFINER and pinned search_path are unchanged.

PUBLIC loses EXECUTE on the seven trigger functions. A trigger fires whatever the privileges on
its function (PostgreSQL checks EXECUTE when a trigger is created, not when it fires), and a
trigger function cannot be called directly, so this takes nothing anyone uses; it only leaves
no privilege that has no purpose. No role is granted EXECUTE on them.

Scope. Every statement names its objects: the nine application tables, alembic_version, the
seven trigger functions and the helper. The only grantees touched are PUBLIC and
ledger_runtime; a grant to any other role (e.g. a monitoring role) is left alone. REVOKE removes
only grants made by the role running it (the owner, or a superuser acting as the owner): a
grant made by another grantor survives it. The privilege tests check the catalog for such
grants, and for anything else beyond this list.

The role must exist and be a NOLOGIN, non-superuser group that is a member of no other role
(through a membership, e.g. in pg_read_all_data, it would inherit more than is granted here):
the migration refuses otherwise (run the bootstrap first). It runs as the owner role (it owns
every object it grants on), or as a superuser.

Downgrade fails closed: ledger_runtime loses every privilege granted here, so the application
stops working, and PUBLIC does NOT get EXECUTE on the trigger functions back. Re-opening a
privilege nobody uses would only weaken the schema.

Like 0001, literals are hard-coded: a migration is a frozen snapshot.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-14 10:00:00.000000
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_GROUP = "ledger_runtime"

SEQUENCE_HELPER = "public.ledger_entries_last_issued_sequence_number()"

TRIGGER_FUNCTIONS = (
    "public.forbid_ledger_mutation()",
    "public.assert_transaction_balanced()",
    "public.forbid_audit_mutation()",
    "public.assert_entry_sequence_order()",
    "public.forbid_idempotency_key_mutation()",
    "public.assert_idempotency_link()",
    "public.forbid_auth_session_changes()",
)

# (table, table-level privileges, INSERT columns, UPDATE columns). Nothing else is granted.
PRIVILEGES: tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    ("users", ("SELECT",), ("id", "email"), ()),
    (
        "user_credentials",
        ("SELECT",),
        ("user_id", "password_hash"),
        ("password_hash", "updated_at"),
    ),
    (
        "auth_sessions",
        ("SELECT",),
        ("id", "user_id", "token_hash", "expires_at"),
        ("revoked_at",),
    ),
    ("ledgers", ("SELECT",), (), ()),
    ("accounts", ("SELECT",), ("id", "ledger_id", "user_id", "kind"), ("balance_minor",)),
    (
        "transactions",
        ("SELECT",),
        ("id", "ledger_id", "kind", "entry_count", "idempotency_user_id", "idempotency_key"),
        (),
    ),
    (
        "ledger_entries",
        ("SELECT",),
        ("id", "transaction_id", "account_id", "ledger_id", "amount_minor"),
        (),
    ),
    (
        "idempotency_keys",
        ("SELECT",),
        ("user_id", "key", "request_fingerprint", "transaction_id"),
        (),
    ),
    (
        "audit_events",
        (),
        (
            "actor_user_id",
            "action",
            "outcome",
            "reason",
            "transaction_id",
            "idempotency_key",
            "details",
        ),
        (),
    ),
)

# Every table whose privileges for PUBLIC and the runtime group this migration decides.
TABLES = (*(f"public.{table}" for table, *_ in PRIVILEGES), "public.alembic_version")


# A plain lower-case identifier: the role name is interpolated into SQL below.
ROLE_NAME_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def require_runtime_group(role: str) -> str:
    """A DO block that refuses to go on unless `role` exists as a NOLOGIN, non-superuser role
    that is a member of no other role: a membership (e.g. pg_read_all_data) would add privileges
    beyond those granted here, unseen by anyone reading this migration."""
    if not ROLE_NAME_PATTERN.fullmatch(role):
        raise ValueError(f"invalid role name {role!r}")
    # A validated identifier (ROLE_NAME_PATTERN), not input: safe to interpolate.
    return f"""
DO $$
DECLARE
    v_can_login boolean;
    v_superuser boolean;
BEGIN
    SELECT rolcanlogin, rolsuper INTO v_can_login, v_superuser
      FROM pg_catalog.pg_roles WHERE rolname = '{role}';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'role {role} does not exist'
            USING HINT = 'Create the database roles first (python -m ledger_api.admin.bootstrap).',
                  ERRCODE = 'undefined_object';
    END IF;
    IF v_can_login OR v_superuser THEN
        RAISE EXCEPTION 'role {role} must be a NOLOGIN, non-superuser group'
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members m
         WHERE m.member = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = '{role}')
    ) THEN
        RAISE EXCEPTION 'role {role} must not be a member of any other role'
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END;
$$
"""  # noqa: S608


def grants(role: str) -> list[str]:
    statements = []
    for table, table_privileges, insert_columns, update_columns in PRIVILEGES:
        if table_privileges:
            statements.append(f'GRANT {", ".join(table_privileges)} ON public.{table} TO "{role}"')
        if insert_columns:
            statements.append(
                f'GRANT INSERT ({", ".join(insert_columns)}) ON public.{table} TO "{role}"'
            )
        if update_columns:
            statements.append(
                f'GRANT UPDATE ({", ".join(update_columns)}) ON public.{table} TO "{role}"'
            )
    statements.append(f'GRANT EXECUTE ON FUNCTION {SEQUENCE_HELPER} TO "{role}"')
    return statements


def upgrade() -> None:
    op.execute(require_runtime_group(RUNTIME_GROUP))
    # Converge: what PUBLIC or the group were granted before (by this grantor) goes first. A
    # table-level REVOKE ALL also revokes the column-level privileges.
    op.execute(f'REVOKE ALL ON TABLE {", ".join(TABLES)} FROM PUBLIC, "{RUNTIME_GROUP}"')
    op.execute(f'REVOKE ALL ON FUNCTION {SEQUENCE_HELPER} FROM PUBLIC, "{RUNTIME_GROUP}"')
    op.execute(f"REVOKE EXECUTE ON FUNCTION {', '.join(TRIGGER_FUNCTIONS)} FROM PUBLIC")
    for statement in grants(RUNTIME_GROUP):
        op.execute(statement)


def downgrade() -> None:
    # Fail closed: the group loses everything granted above; PUBLIC's EXECUTE on the trigger
    # functions is NOT restored. A group that no longer exists holds nothing to revoke.
    op.execute(
        f"""
DO $$
BEGIN
    IF pg_catalog.to_regrole('{RUNTIME_GROUP}') IS NOT NULL THEN
        EXECUTE 'REVOKE ALL ON TABLE {", ".join(TABLES)} FROM "{RUNTIME_GROUP}"';
        EXECUTE 'REVOKE ALL ON FUNCTION {SEQUENCE_HELPER} FROM "{RUNTIME_GROUP}"';
    END IF;
END;
$$
"""
    )
