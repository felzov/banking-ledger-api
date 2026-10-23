"""The runtime role's effective privileges after migrations 0013 and 0014 (Phase 10).

Setup, as in a deployment: the role bootstrap (unique test names for the owner and the login
role), migrations run by the owner role, and a login role that inherits ledger_runtime, the
cluster-wide group 0013 grants to. The bootstrap is given a unique group name too: roles are
cluster-wide, and the real group may have other members. Its two effects on the real group
(the app's membership, schema USAGE) are therefore made here, explicitly.

Expectations come from tests.database.RUNTIME_PRIVILEGES, written independently of the
migration, never from the migration's own lists.
"""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.admin.bootstrap import DEFAULT_ROLE_NAMES, RoleNames
from ledger_api.config import Settings
from ledger_api.data.errors import sqlstate
from ledger_api.main import create_app
from tests.auth_helpers import TEST_PASSWORD
from tests.conftest import serve, settings_for
from tests.database import (
    ALEMBIC_INI,
    RUNTIME_GROUP,
    RUNTIME_PRIVILEGES,
    SEQUENCE_HELPER,
    assert_reconciled,
    configured_database_url,
)
from tests.integration.test_role_bootstrap import (
    APP_PASSWORD,
    OWNER_PASSWORD,
    Cluster,
    _as,
    _bootstrap,
    _engine,
    _migrate,
    _query,
    _query_commit,
    _scalar,
)

TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
TRIGGER_FUNCTIONS = {
    "forbid_ledger_mutation",
    "assert_transaction_balanced",
    "forbid_audit_mutation",
    "assert_entry_sequence_order",
    "forbid_idempotency_key_mutation",
    "assert_idempotency_link",
    "forbid_auth_session_changes",
}


# --- setup ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeDatabase:
    url: URL
    names: RoleNames

    @property
    def app_url(self) -> URL:
        return _as(self.url, self.names.app, APP_PASSWORD)

    @property
    def owner_url(self) -> URL:
        return _as(self.url, self.names.owner, OWNER_PASSWORD)


async def _deploy(
    cluster: Cluster, name: str, *, upgrade_from_0012: bool = False
) -> RuntimeDatabase:
    """A database deployed like production: bootstrapped, migrated by the owner role, with the
    app logging in as a member of the runtime group.

    upgrade_from_0012: an existing volume instead, migrated to 0012 by the admin (before the
    roles existed), then bootstrapped (ownership transfer) and upgraded by the owner.
    """
    url = await cluster.database(name)
    names = cluster.role_names()
    if upgrade_from_0012:
        await _migrate(url, "0012")
    await _bootstrap(url, names)
    await _migrate(_as(url, names.owner, OWNER_PASSWORD))
    await _attach_app(url, names)
    return RuntimeDatabase(url, names)


async def _attach_app(url: URL, names: RoleNames) -> None:
    # What the bootstrap does for its runtime group, for the real one (see the module docstring).
    await _query_commit(
        url,
        f'GRANT "{RUNTIME_GROUP}" TO "{names.app}" WITH INHERIT TRUE, SET FALSE, ADMIN FALSE',
        f'GRANT USAGE ON SCHEMA public TO "{RUNTIME_GROUP}"',
    )


@pytest.fixture(scope="module")
async def runtime_db() -> AsyncIterator[RuntimeDatabase]:
    cluster = Cluster(configured_database_url())
    try:
        yield await _deploy(cluster, "runtime_privileges")
    finally:
        await cluster.cleanup()


@pytest.fixture
async def cluster() -> AsyncIterator[Cluster]:
    cluster = Cluster(configured_database_url())
    try:
        yield cluster
    finally:
        await cluster.cleanup()


def _migration_0013() -> ModuleType:
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_revision("0013")
    assert script is not None
    return script.module


# Every ACL entry in public that names PUBLIC or the runtime group: tables, columns, functions,
# sequences. by_owner: granted by the object's owner (anything else survives 0013's REVOKEs).
RUNTIME_ACL = """
SELECT o.kind, o.object, o.privilege_type, o.is_grantable, o.by_owner,
       CASE WHEN o.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(o.grantee) END AS grantee,
       pg_get_userbyid(o.grantor) AS grantor
  FROM (
    SELECT CASE WHEN c.relkind = 'S' THEN 'sequence' ELSE 'table' END AS kind,
           c.relname::text AS object, a.privilege_type, a.is_grantable,
           a.grantor = c.relowner AS by_owner, a.grantee, a.grantor
      FROM pg_class c, aclexplode(c.relacl) a
     WHERE c.relnamespace = 'public'::regnamespace
    UNION ALL
    SELECT 'column', c.relname || '.' || at.attname, a.privilege_type, a.is_grantable,
           a.grantor = c.relowner, a.grantee, a.grantor
      FROM pg_attribute at JOIN pg_class c ON c.oid = at.attrelid, aclexplode(at.attacl) a
     WHERE c.relnamespace = 'public'::regnamespace AND at.attnum > 0 AND NOT at.attisdropped
    UNION ALL
    SELECT 'function', p.proname::text, a.privilege_type, a.is_grantable,
           a.grantor = p.proowner, a.grantee, a.grantor
      FROM pg_proc p, aclexplode(p.proacl) a
     WHERE p.pronamespace = 'public'::regnamespace
  ) o
 WHERE o.grantee = 0 OR o.grantee = to_regrole(:group)
 ORDER BY 1, 2, 3, 6
"""


async def _runtime_acl(url: URL) -> set[tuple[Any, ...]]:
    """The ACL entries for PUBLIC and the runtime group, without grantor names (they differ
    between databases: each has its own owner role)."""
    rows = await _query(url, RUNTIME_ACL, group=RUNTIME_GROUP)
    return {
        (row.kind, row.object, row.privilege_type, row.is_grantable, row.by_owner, row.grantee)
        for row in rows
    }


async def unexpected_runtime_grants(url: URL) -> list[str]:
    """Every grant to PUBLIC or the runtime group that 0013 did not make: from another grantor,
    delegable, or to PUBLIC at all. REVOKE only removes a grantor's own grants, so this is how
    the survivors are found."""
    return sorted(
        f"{row.privilege_type} on {row.kind} {row.object} to {row.grantee} "
        f"(granted by {row.grantor}{', grantable' if row.is_grantable else ''})"
        for row in await _query(url, RUNTIME_ACL, group=RUNTIME_GROUP)
        if row.grantee == "PUBLIC" or not row.by_owner or row.is_grantable
    )


# --- the catalog ----------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["group", "app"])
async def test_table_and_column_privileges_are_exactly_the_matrix(
    runtime_db: RuntimeDatabase, who: str
) -> None:
    # "app" checks the login role: what it inherits from the group, and nothing more.
    role = RUNTIME_GROUP if who == "group" else runtime_db.names.app
    tables = {
        row[0]
        for row in await _query(
            runtime_db.url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }
    assert tables == set(RUNTIME_PRIVILEGES)  # a new table needs an explicit decision

    granted = await _query(
        runtime_db.url,
        "SELECT c.relname AS table_name, p.privilege "
        "FROM pg_class c, unnest(CAST(:privileges AS text[])) p(privilege) "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' "
        "AND has_table_privilege(:role, c.oid, p.privilege)",
        role=role,
        privileges=list(TABLE_PRIVILEGES),
    )
    columns = await _query(
        runtime_db.url,
        "SELECT c.relname AS table_name, at.attname AS column_name, p.privilege "
        "FROM pg_class c JOIN pg_attribute at ON at.attrelid = c.oid, "
        "unnest(ARRAY['INSERT', 'UPDATE', 'SELECT', 'REFERENCES']) p(privilege) "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' "
        "AND at.attnum > 0 AND NOT at.attisdropped "
        "AND has_column_privilege(:role, c.oid, at.attnum, p.privilege)",
        role=role,
    )

    expected_tables = {
        (t, p) for t, privileges in RUNTIME_PRIVILEGES.items() for p in privileges.table
    }
    assert {(row.table_name, row.privilege) for row in granted} == expected_tables
    by_column = {(row.table_name, row.column_name, row.privilege) for row in columns}
    for table, privileges in RUNTIME_PRIVILEGES.items():
        table_columns = {
            row[0]
            for row in await _query(
                runtime_db.url,
                "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(:t) "
                "AND attnum > 0 AND NOT attisdropped",
                t=f"public.{table}",
            )
        }
        for column in table_columns:
            for privilege, allowed in (
                ("INSERT", privileges.insert),
                ("UPDATE", privileges.update),
            ):
                assert ((table, column, privilege) in by_column) == (column in allowed), (
                    f"{privilege} on {table}.{column}"
                )
            assert ((table, column, "SELECT") in by_column) == ("SELECT" in privileges.table)
            assert (table, column, "REFERENCES") not in by_column


async def test_no_grant_beyond_0013_survives_anywhere(runtime_db: RuntimeDatabase) -> None:
    # No PUBLIC entry, no grant option, no grant from anyone but the owner, on any table,
    # column, sequence or function in public.
    assert await unexpected_runtime_grants(runtime_db.url) == []


async def test_only_the_sequence_helper_is_executable(runtime_db: RuntimeDatabase) -> None:
    functions = await _query(
        runtime_db.url,
        "SELECT p.proname, p.prorettype = 'trigger'::regtype AS is_trigger, "
        "has_function_privilege(:group, p.oid, 'EXECUTE') AS runtime, "
        "has_function_privilege(:app, p.oid, 'EXECUTE') AS app, "
        "p.proacl IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee = 0) AS public_revoked "
        "FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace",
        group=RUNTIME_GROUP,
        app=runtime_db.names.app,
    )
    assert {row.proname for row in functions if row.is_trigger} == TRIGGER_FUNCTIONS
    for row in functions:
        helper = row.proname == "ledger_entries_last_issued_sequence_number"
        assert (row.runtime, row.app) == (helper, helper), row.proname
        assert row.public_revoked, row.proname  # a NULL ACL would mean PUBLIC EXECUTE

    helper = (
        await _query(
            runtime_db.url,
            "SELECT p.proowner = c.relowner AS owned_by_table_owner, p.prosecdef, p.proconfig "
            "FROM pg_proc p, pg_class c "
            "WHERE p.oid = to_regprocedure(:helper) AND c.oid = 'public.ledger_entries'::regclass",
            helper=SEQUENCE_HELPER,
        )
    )[0]
    assert (helper.owned_by_table_owner, helper.prosecdef) == (True, True)
    assert helper.proconfig == ["search_path=pg_catalog, pg_temp"]


@pytest.mark.parametrize("who", ["group", "app"])
async def test_no_privilege_on_any_sequence(runtime_db: RuntimeDatabase, who: str) -> None:
    role = RUNTIME_GROUP if who == "group" else runtime_db.names.app
    sequences = await _query(
        runtime_db.url,
        "SELECT c.relname, has_sequence_privilege(:role, c.oid, 'USAGE') "
        "OR has_sequence_privilege(:role, c.oid, 'SELECT') "
        "OR has_sequence_privilege(:role, c.oid, 'UPDATE') AS any_privilege "
        "FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'S'",
        role=role,
    )
    assert {row.relname for row in sequences} == {
        "audit_events_id_seq",
        "ledger_entries_sequence_number_seq",
    }
    assert not any(row.any_privilege for row in sequences)


async def test_runtime_roles_own_nothing_and_cannot_create(runtime_db: RuntimeDatabase) -> None:
    for role in (RUNTIME_GROUP, runtime_db.names.app):
        owned = await _scalar(
            runtime_db.url,
            "SELECT count(*) FROM pg_shdepend s WHERE s.deptype = 'o' "
            "AND s.refobjid = to_regrole(:role) AND s.dbid IN (0, "
            "(SELECT oid FROM pg_database WHERE datname = current_database()))",
            role=role,
        )
        assert owned == 0, role
        create, usage = (
            await _query(
                runtime_db.url,
                "SELECT has_schema_privilege(:role, 'public', 'CREATE'), "
                "has_schema_privilege(:role, 'public', 'USAGE')",
                role=role,
            )
        )[0]
        assert (create, usage) == (False, True), role


async def test_privileges_come_only_through_inheritance(
    runtime_db: RuntimeDatabase, cluster: Cluster
) -> None:
    inheriting, not_inheriting = cluster.role(), cluster.role()
    await _query_commit(
        runtime_db.url,
        f'CREATE ROLE "{inheriting}" NOLOGIN',
        f'CREATE ROLE "{not_inheriting}" NOLOGIN',
        f'GRANT "{RUNTIME_GROUP}" TO "{inheriting}" WITH INHERIT TRUE, SET FALSE',
        f'GRANT "{RUNTIME_GROUP}" TO "{not_inheriting}" WITH INHERIT FALSE, SET FALSE',
    )
    check = "SELECT has_table_privilege(:role, 'public.users', 'SELECT')"
    assert await _scalar(runtime_db.url, check, role=inheriting) is True
    assert await _scalar(runtime_db.url, check, role=not_inheriting) is False


# --- what the application's login role cannot do --------------------------------------------------

APPLICATION_TABLES = sorted(RUNTIME_PRIVILEGES)

DENIED = [
    # Table names from RUNTIME_PRIVILEGES, a test constant.
    *[(f"delete-{t}", f"DELETE FROM public.{t}") for t in APPLICATION_TABLES],  # noqa: S608
    *[(f"truncate-{t}", f"TRUNCATE public.{t}") for t in APPLICATION_TABLES],
    ("update-users-email", "UPDATE public.users SET email = 'x@example.com'"),
    ("update-accounts-owner", "UPDATE public.accounts SET user_id = NULL"),
    ("update-accounts-kind", "UPDATE public.accounts SET kind = 'system'"),
    ("update-accounts-ledger", "UPDATE public.accounts SET ledger_id = uuidv7()"),
    ("update-transactions", "UPDATE public.transactions SET kind = 'deposit'"),
    ("update-ledger-entries", "UPDATE public.ledger_entries SET amount_minor = 1"),
    ("update-credentials-owner", "UPDATE public.user_credentials SET user_id = uuidv7()"),
    ("update-credentials-created", "UPDATE public.user_credentials SET created_at = now()"),
    ("update-sessions-expiry", "UPDATE public.auth_sessions SET expires_at = now()"),
    ("update-sessions-owner", "UPDATE public.auth_sessions SET user_id = uuidv7()"),
    ("update-keys", "UPDATE public.idempotency_keys SET key = 'k'"),
    ("update-audit", "UPDATE public.audit_events SET reason = 'x'"),
    ("update-ledgers", "UPDATE public.ledgers SET currency = 'GBP'"),
    ("insert-ledgers", "INSERT INTO public.ledgers (currency) VALUES ('USD')"),
    (
        "insert-account-with-balance",
        "INSERT INTO public.accounts (id, ledger_id, user_id, kind, balance_minor) "
        "VALUES (uuidv7(), uuidv7(), uuidv7(), 'customer', 100)",
    ),
    (
        "insert-account-backdated",
        "INSERT INTO public.accounts (id, ledger_id, user_id, kind, created_at) "
        "VALUES (uuidv7(), uuidv7(), uuidv7(), 'customer', now())",
    ),
    (
        "insert-user-backdated",
        "INSERT INTO public.users (id, email, created_at) "
        "VALUES (uuidv7(), 'a@example.com', now())",
    ),
    (
        "insert-transaction-backdated",
        "INSERT INTO public.transactions (id, ledger_id, kind, entry_count, created_at) "
        "VALUES (uuidv7(), uuidv7(), 'deposit', 2, now())",
    ),
    (
        "insert-entry-with-number",
        "INSERT INTO public.ledger_entries "
        "(id, transaction_id, account_id, ledger_id, amount_minor, sequence_number) "
        "OVERRIDING SYSTEM VALUE VALUES (uuidv7(), uuidv7(), uuidv7(), uuidv7(), 1, 1)",
    ),
    (
        "insert-audit-backdated",
        "INSERT INTO public.audit_events (action, outcome, details, occurred_at) "
        "VALUES ('user.register', 'succeeded', '{}', now())",
    ),
    (
        "insert-audit-with-id",
        "INSERT INTO public.audit_events (id, action, outcome, details) OVERRIDING SYSTEM VALUE "
        "VALUES (1, 'user.register', 'succeeded', '{}')",
    ),
    (
        "insert-audit-returning",
        "INSERT INTO public.audit_events (action, outcome, reason, details) "
        "VALUES ('user.register', 'rejected', 'x', '{}') RETURNING id",
    ),
    (
        "insert-session-revoked",
        "INSERT INTO public.auth_sessions (id, user_id, token_hash, expires_at, revoked_at) "
        "VALUES (uuidv7(), uuidv7(), '\\x00', now(), now())",
    ),
    (
        "insert-session-backdated",
        "INSERT INTO public.auth_sessions (id, user_id, token_hash, expires_at, created_at) "
        "VALUES (uuidv7(), uuidv7(), '\\x00', now(), now())",
    ),
    (
        "insert-credential-dated",
        "INSERT INTO public.user_credentials (user_id, password_hash, updated_at) "
        "VALUES (uuidv7(), 'x', now())",
    ),
    (
        "insert-key-backdated",
        "INSERT INTO public.idempotency_keys "
        "(user_id, key, request_fingerprint, transaction_id, created_at) "
        "VALUES (uuidv7(), 'k', '\\x00', uuidv7(), now())",
    ),
    ("select-audit", "SELECT 1 FROM public.audit_events"),
    ("select-alembic-version", "SELECT 1 FROM public.alembic_version"),
    ("nextval-audit", "SELECT nextval('public.audit_events_id_seq')"),
    (
        "nextval-entries",
        "SELECT nextval(pg_get_serial_sequence('public.ledger_entries', 'sequence_number'))",
    ),
    (
        "setval-entries",
        "SELECT setval(pg_get_serial_sequence('public.ledger_entries', 'sequence_number'), 1)",
    ),
    ("call-trigger-function", "DO $$ BEGIN PERFORM public.forbid_ledger_mutation(); END $$"),
    ("create-table", "CREATE TABLE public.intruder (id int)"),
    ("create-function", "CREATE FUNCTION public.intruder() RETURNS int LANGUAGE sql AS 'SELECT 1'"),
    ("create-temporary-table", "CREATE TEMPORARY TABLE intruder (id int)"),
    ("set-role-group", f'SET ROLE "{RUNTIME_GROUP}"'),
    ("skip-triggers", "SET session_replication_role = replica"),
]


@pytest.mark.parametrize("statement", [s for _, s in DENIED], ids=[i for i, _ in DENIED])
async def test_the_app_is_refused(runtime_db: RuntimeDatabase, statement: str) -> None:
    # 42501, by privilege: before any constraint, trigger or row is looked at.
    engine = _engine(runtime_db.app_url)
    try:
        async with engine.connect() as connection, connection.begin():
            with pytest.raises(DBAPIError) as excinfo:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()
    assert sqlstate(excinfo.value) == "42501", excinfo.value


async def test_the_app_may_record_a_request_id(runtime_db: RuntimeDatabase) -> None:
    # Migration 0014 (ADR 0017): the one column added to what the app may insert. Reading the
    # event back (RETURNING, SELECT) stays refused above.
    engine = _engine(runtime_db.app_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "INSERT INTO public.audit_events "
                    "(action, outcome, reason, details, request_id) "
                    "VALUES ('user.register', 'rejected', 'x', '{}', 'r')"
                )
            )
            await transaction.rollback()
    finally:
        await engine.dispose()


async def test_the_app_cannot_read_a_sequence(runtime_db: RuntimeDatabase) -> None:
    # pg_sequence_last_value answers NULL, not an error, without the privilege (migration 0007).
    value = await _scalar(
        runtime_db.app_url,
        "SELECT pg_sequence_last_value("
        "pg_get_serial_sequence('public.ledger_entries', 'sequence_number')::regclass)",
    )
    assert value is None


# --- what it can do: the whole API, as the runtime role ------------------------------------------


async def test_the_whole_api_works_as_the_runtime_role(runtime_db: RuntimeDatabase) -> None:
    token = secrets.token_hex(4)
    app = create_app(settings_for(runtime_db.app_url))
    # A second app hashing with other parameters: logging in through it rehashes the password
    # (UPDATE password_hash, updated_at).
    rehashing = create_app(
        Settings(
            app_env="test",
            database_url=SecretStr(runtime_db.app_url.render_as_string(hide_password=False)),
            argon2_time_cost=2,
            argon2_memory_cost_kib=8,
            argon2_parallelism=1,
        )
    )
    async with serve(app) as client, serve(rehashing) as rehash_client:
        assert await _scalar(runtime_db.app_url, "SELECT current_user") == runtime_db.names.app

        async def register(name: str) -> tuple[str, dict[str, str], str]:
            email = f"{name}-{token}@example.com"
            created = await client.post("/users", json={"email": email, "password": TEST_PASSWORD})
            assert created.status_code == 201, created.text
            login = await client.post("/sessions", json={"email": email, "password": TEST_PASSWORD})
            assert login.status_code == 201, login.text
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            user_id = created.json()["id"]
            opened = await client.post(
                f"/users/{user_id}/accounts", json={"currency": "GBP"}, headers=headers
            )
            assert opened.status_code == 201, opened.text
            return user_id, headers, opened.json()["id"]

        alice, alice_headers, alice_account = await register("alice")
        _bob, _bob_headers, bob_account = await register("bob")
        base = f"/users/{alice}/accounts/{alice_account}"

        def keyed(key: str) -> dict[str, str]:
            return {**alice_headers, "Idempotency-Key": f"{key}-{token}"}

        deposit = await client.post(
            f"{base}/deposits", json={"amount_minor": 5_000}, headers=keyed("d")
        )
        assert deposit.status_code == 201, deposit.text
        replay = await client.post(
            f"{base}/deposits", json={"amount_minor": 5_000}, headers=keyed("d")
        )
        assert replay.headers.get("Idempotent-Replayed") == "true"
        assert replay.json()["transaction_id"] == deposit.json()["transaction_id"]
        withdrawal = await client.post(
            f"{base}/withdrawals", json={"amount_minor": 1_000}, headers=keyed("w")
        )
        assert withdrawal.status_code == 201, withdrawal.text
        rejected = await client.post(
            f"{base}/withdrawals", json={"amount_minor": 999_999}, headers=keyed("x")
        )
        assert rejected.status_code == 422, rejected.text  # audited after the rollback
        transfer = await client.post(
            f"{base}/transfers",
            json={"amount_minor": 500, "destination_account_id": bob_account},
            headers=keyed("t"),
        )
        assert transfer.status_code == 201, transfer.text

        statement = await client.get(f"{base}/statement", headers=alice_headers)
        assert statement.status_code == 200, statement.text
        assert [e["amount_minor"] for e in statement.json()["entries"]] == [-500, -1_000, 5_000]
        read = await client.get(
            f"{base}/transactions/{deposit.json()['transaction_id']}", headers=alice_headers
        )
        assert read.status_code == 200, read.text

        email = f"alice-{token}@example.com"
        relogin = await rehash_client.post(
            "/sessions", json={"email": email, "password": TEST_PASSWORD}
        )
        assert relogin.status_code == 201, relogin.text
        logout = await client.delete("/sessions/current", headers=alice_headers)
        assert logout.status_code == 204, logout.text
        again = await client.get(f"/users/{alice}", headers=alice_headers)
        assert again.status_code == 401

    # Checked as the admin: the ledger is consistent, every step left its audit event, the
    # password was rehashed.
    engine = _engine(runtime_db.url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            await assert_reconciled(session)
    finally:
        await engine.dispose()
    events = await _query(
        runtime_db.url,
        "SELECT action, outcome FROM audit_events WHERE actor_user_id = CAST(:user AS uuid) "
        "ORDER BY id",
        user=alice,
    )
    assert [(row.action, row.outcome) for row in events] == [
        ("user.register", "succeeded"),
        ("auth.login", "succeeded"),
        ("account.open", "succeeded"),
        ("posting.deposit", "succeeded"),
        ("posting.withdrawal", "succeeded"),
        ("posting.withdrawal", "rejected"),
        ("posting.transfer", "succeeded"),
        ("auth.login", "succeeded"),
        ("auth.logout", "succeeded"),
    ]
    rehashed = await _scalar(
        runtime_db.url,
        "SELECT c.password_hash LIKE '%t=2%' AND c.updated_at > c.created_at "
        "FROM user_credentials c WHERE c.user_id = CAST(:user AS uuid)",
        user=alice,
    )
    assert rehashed is True


# --- migration paths ------------------------------------------------------------------------------


async def test_an_upgraded_volume_gets_exactly_the_fresh_privileges(
    runtime_db: RuntimeDatabase, cluster: Cluster
) -> None:
    upgraded = await _deploy(cluster, "runtime_upgraded", upgrade_from_0012=True)

    assert await _runtime_acl(upgraded.url) == await _runtime_acl(runtime_db.url)
    assert await unexpected_runtime_grants(upgraded.url) == []


async def test_downgrade_fails_closed_and_upgrade_restores(
    runtime_db: RuntimeDatabase, cluster: Cluster
) -> None:
    deployed = await _deploy(cluster, "runtime_downgrade")
    fresh = await _runtime_acl(runtime_db.url)

    await _migrate(deployed.owner_url, "0012", down=True)
    after_downgrade = await _runtime_acl(deployed.url)
    # The group holds nothing anywhere; PUBLIC did NOT get EXECUTE on the trigger functions back.
    assert not [entry for entry in after_downgrade if entry[5] == RUNTIME_GROUP]
    assert not [entry for entry in after_downgrade if entry[5] == "PUBLIC"]
    public_execute = await _scalar(
        deployed.url,
        "SELECT count(*) FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace "
        "AND (p.proacl IS NULL "
        "OR EXISTS (SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee = 0))",
    )
    assert public_execute == 0
    denied = _engine(deployed.app_url)
    try:
        async with denied.connect() as connection, connection.begin():
            with pytest.raises(DBAPIError) as excinfo:
                await connection.execute(text("SELECT 1 FROM public.users"))
    finally:
        await denied.dispose()
    assert sqlstate(excinfo.value) == "42501"

    await _migrate(deployed.owner_url)
    assert await _runtime_acl(deployed.url) == fresh


async def test_upgrade_converges_grants_the_owner_made_before(
    runtime_db: RuntimeDatabase, cluster: Cluster
) -> None:
    url = await cluster.database("runtime_converge")
    names = cluster.role_names()
    await _bootstrap(url, names)
    owner_url = _as(url, names.owner, OWNER_PASSWORD)
    await _migrate(owner_url, "0012")
    await _query_commit(
        owner_url,  # the owner, i.e. the grantor 0013 runs as
        f'GRANT DELETE, TRUNCATE ON public.users TO "{RUNTIME_GROUP}"',
        "GRANT SELECT ON public.audit_events TO PUBLIC",
        f'GRANT SELECT ON public.alembic_version TO "{RUNTIME_GROUP}" WITH GRANT OPTION',
        f'GRANT UPDATE (email) ON public.users TO "{RUNTIME_GROUP}"',
        "GRANT EXECUTE ON FUNCTION public.ledger_entries_last_issued_sequence_number() TO PUBLIC",
    )

    await _migrate(owner_url)

    assert await _runtime_acl(url) == await _runtime_acl(runtime_db.url)


async def test_grants_from_another_grantor_survive_and_are_detected(cluster: Cluster) -> None:
    # The documented limit of REVOKE: it removes only its grantor's grants. A role the owner
    # delegated to keeps what it granted; the catalog check finds it.
    deployed = await _deploy(cluster, "runtime_foreign_grantor")
    delegate = cluster.role()
    await _query_commit(
        deployed.url,
        f'CREATE ROLE "{delegate}" NOLOGIN',
        f'GRANT USAGE ON SCHEMA public TO "{delegate}"',  # PUBLIC has none since the bootstrap
        f'GRANT SELECT ON public.audit_events TO "{delegate}" WITH GRANT OPTION',
        f'SET ROLE "{delegate}"',
        f'GRANT SELECT ON public.audit_events TO "{RUNTIME_GROUP}"',
        "RESET ROLE",
    )
    await _migrate(deployed.owner_url, "0012", down=True)
    await _migrate(deployed.owner_url)  # 0013's REVOKEs run again, as the owner

    assert await unexpected_runtime_grants(deployed.url) == [
        f"SELECT on table audit_events to {RUNTIME_GROUP} (granted by {delegate})"
    ]


async def test_the_migration_refuses_an_unsafe_runtime_group(cluster: Cluster) -> None:
    migration = _migration_0013()
    url = await cluster.database("runtime_guard")
    login, member = cluster.role(), cluster.role()
    await _query_commit(
        url,
        f'CREATE ROLE "{login}" LOGIN',
        f'CREATE ROLE "{member}" NOLOGIN',
        # Would give the app SELECT on every table, audit_events included.
        f'GRANT pg_read_all_data TO "{member}"',
    )

    cases = ((f"lbt_{secrets.token_hex(5)}_missing", "42704"), (login, "55000"), (member, "55000"))
    for role, expected in cases:
        engine = _engine(url)
        try:
            async with engine.connect() as connection, connection.begin():
                with pytest.raises(DBAPIError) as excinfo:
                    await connection.execute(text(migration.require_runtime_group(role)))
        finally:
            await engine.dispose()
        assert sqlstate(excinfo.value) == expected
    with pytest.raises(ValueError, match="invalid role name"):
        migration.require_runtime_group("ledger_runtime'; DROP TABLE users; --")


def test_the_migration_grants_to_the_bootstraps_runtime_group() -> None:
    # The name is hard-coded in both (a migration is a frozen snapshot): they must agree.
    assert _migration_0013().RUNTIME_GROUP == DEFAULT_ROLE_NAMES.runtime_group == RUNTIME_GROUP
