"""The production sequence, end to end, with the real role names (Phase 10 E6, ADR 0015):

    bootstrap CLI (admin) -> alembic CLI (ledger_owner) -> API (ledger_app)

The real names (ledger_owner, ledger_app, ledger_runtime) are cluster-wide roles: they are used
only here, on the disposable test cluster (tests.database refuses any other), and stay there,
in its tmpfs data, until the container goes. Each database is a disposable one of its own.
"""

import asyncio
import os
import secrets
import uuid
from collections.abc import AsyncIterator

import pytest
from alembic import command
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.admin import bootstrap as bootstrap_module
from ledger_api.admin.bootstrap import DEFAULT_ROLE_NAMES
from ledger_api.main import create_app
from tests.auth_helpers import TEST_PASSWORD
from tests.conftest import serve, settings_for
from tests.database import (
    DISPOSABLE_CLUSTER_VARIABLE,
    DISPOSABLE_SERVER_SETTING,
    RUNTIME_PRIVILEGES,
    assert_disposable_cluster,
    assert_reconciled,
    configured_database_url,
    run_alembic,
)
from tests.integration.test_migration_guard import _alembic_cli
from tests.integration.test_role_bootstrap import (
    Cluster,
    _engine,
    _owners_in_public,
    _query,
    _query_commit,
    _scalar,
)
from tests.integration.test_runtime_privileges import unexpected_runtime_grants

OWNER_PASSWORD = "synthetic-real-owner-password"  # noqa: S105 (synthetic test value)
APP_PASSWORD = "synthetic-real-app-password-1"  # noqa: S105 (synthetic test value)


@pytest.fixture
async def cluster() -> AsyncIterator[Cluster]:
    cluster = Cluster(configured_database_url())
    try:
        yield cluster
    finally:
        await cluster.cleanup()  # its databases; the real roles stay on the disposable cluster


def _owner(url: URL) -> URL:
    return url.set(username=DEFAULT_ROLE_NAMES.owner, password=OWNER_PASSWORD)


def _app(url: URL) -> URL:
    return url.set(username=DEFAULT_ROLE_NAMES.app, password=APP_PASSWORD)


async def _run_bootstrap_cli(
    url: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    """python -m ledger_api.admin.bootstrap, configured as the bootstrap service is."""
    monkeypatch.setenv("ADMIN_DATABASE_URL", url.render_as_string(hide_password=False))
    monkeypatch.setenv("LEDGER_OWNER_PASSWORD", OWNER_PASSWORD)
    monkeypatch.setenv("LEDGER_APP_PASSWORD", APP_PASSWORD)
    # main() runs its own event loop: in a thread, away from the test's.
    assert await asyncio.to_thread(bootstrap_module.main) == 0
    out, err = capsys.readouterr()
    # The admin URL has no password in CI (trust auth keeps every credential out of git).
    for secret in (OWNER_PASSWORD, APP_PASSWORD, url.password):
        if secret is not None:
            assert secret not in out + err
    return out


async def _migrate_as_owner(url: URL) -> None:
    result = await asyncio.to_thread(_alembic_cli, _owner(url), "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert OWNER_PASSWORD not in result.stdout + result.stderr


async def _assert_owned_by_ledger_owner(url: URL) -> None:
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == "0013"
    assert await _owners_in_public(url) == {DEFAULT_ROLE_NAMES.owner}
    roles = {
        row.rolname: row
        for row in await _query(
            url,
            "SELECT rolname, rolsuper, rolcanlogin FROM pg_roles WHERE rolname = ANY(:n)",
            n=list(DEFAULT_ROLE_NAMES.all()),
        )
    }
    assert {name: (r.rolsuper, r.rolcanlogin) for name, r in roles.items()} == {
        DEFAULT_ROLE_NAMES.owner: (False, True),
        DEFAULT_ROLE_NAMES.app: (False, True),
        DEFAULT_ROLE_NAMES.runtime_group: (False, False),
    }


async def _use_the_api_as_ledger_app(url: URL) -> str:
    """Register, log in, open an account, post a keyed deposit and read it back, through an app
    built as uvicorn builds it (privilege guard on), connected as ledger_app."""
    app = create_app(settings_for(_app(url)))
    token = secrets.token_hex(4)
    email = f"wiring-{token}@example.com"
    async with serve(app) as client:
        async with app.state.engine.connect() as connection:
            identity = tuple(
                (await connection.execute(text("SELECT current_user, session_user"))).one()
            )
        assert identity == (DEFAULT_ROLE_NAMES.app, DEFAULT_ROLE_NAMES.app)
        # Ready: the startup privilege guard accepted ledger_app's connection.
        ready = await client.get("/health/ready")
        assert (ready.status_code, ready.json()) == (200, {"status": "ok"})

        created = await client.post("/users", json={"email": email, "password": TEST_PASSWORD})
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]
        login = await client.post("/sessions", json={"email": email, "password": TEST_PASSWORD})
        assert login.status_code == 201, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        opened = await client.post(
            f"/users/{user_id}/accounts", json={"currency": "GBP"}, headers=headers
        )
        assert opened.status_code == 201, opened.text
        base = f"/users/{user_id}/accounts/{opened.json()['id']}"
        keyed = {**headers, "Idempotency-Key": f"wiring-{token}"}
        deposit = await client.post(f"{base}/deposits", json={"amount_minor": 2_500}, headers=keyed)
        assert deposit.status_code == 201, deposit.text
        replay = await client.post(f"{base}/deposits", json={"amount_minor": 2_500}, headers=keyed)
        assert replay.headers.get("Idempotent-Replayed") == "true"
        statement = await client.get(f"{base}/statement", headers=headers)
        assert statement.json()["balance_minor"] == 2_500
    return str(user_id)


async def _assert_consistent_and_audited(url: URL, user_id: str) -> None:
    engine = _engine(url)  # the admin, to check what the application may not read
    try:
        async with AsyncSession(engine) as session, session.begin():
            await assert_reconciled(session)
    finally:
        await engine.dispose()
    actions = await _query(
        url,
        "SELECT action FROM audit_events WHERE actor_user_id = CAST(:u AS uuid) ORDER BY id",
        u=user_id,
    )
    assert [row.action for row in actions] == [
        "user.register",
        "auth.login",
        "account.open",
        "posting.deposit",
    ]


async def test_a_fresh_database_bootstrapped_migrated_and_served(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = await cluster.database("wiring_fresh")

    await _run_bootstrap_cli(url, monkeypatch, capsys)
    await _migrate_as_owner(url)
    await _assert_owned_by_ledger_owner(url)
    user_id = await _use_the_api_as_ledger_app(url)
    await _assert_consistent_and_audited(url, user_id)

    # Running the sequence again changes nothing (both steps are idempotent).
    again = await _run_bootstrap_cli(url, monkeypatch, capsys)
    assert "roles created: none" in again
    assert "objects transferred to ledger_owner: 0" in again
    await _migrate_as_owner(url)
    await _assert_owned_by_ledger_owner(url)


async def test_an_existing_volume_upgraded_in_place(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Today's volumes: migrated to 0012 by the admin superuser, with data in them.
    url = await cluster.database("wiring_existing")
    engine = _engine(url)
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0012"))
    finally:
        await engine.dispose()
    await _query_commit(url, "INSERT INTO users (email) VALUES ('existing-user@example.com')")

    out = await _run_bootstrap_cli(url, monkeypatch, capsys)
    assert "objects transferred to ledger_owner: 0" not in out  # everything was the admin's
    await _migrate_as_owner(url)

    await _assert_owned_by_ledger_owner(url)
    assert (
        await _scalar(url, "SELECT count(*) FROM users WHERE email = 'existing-user@example.com'")
        == 1
    )
    user_id = await _use_the_api_as_ledger_app(url)
    await _assert_consistent_and_audited(url, user_id)


# --- the development volume's own path: 0007 -> bootstrap -> 0008-0013 ------------------------

# Every table and routine migration 0007 leaves in public: what the bootstrap must transfer.
OBJECTS_AT_0007 = {
    ("table", "accounts"),
    ("table", "alembic_version"),
    ("table", "audit_events"),
    ("table", "ledger_entries"),
    ("table", "ledgers"),
    ("table", "transactions"),
    ("table", "users"),
    ("function", "assert_entry_sequence_order"),
    ("function", "assert_transaction_balanced"),
    ("function", "forbid_audit_mutation"),
    ("function", "forbid_ledger_mutation"),
    ("function", "ledger_entries_last_issued_sequence_number"),
}

OWNED_TABLES_AND_ROUTINES = """
SELECT 'table' AS kind, relname::text AS name, pg_get_userbyid(relowner) AS owner FROM pg_class
 WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'
UNION ALL
SELECT 'function', proname::text, pg_get_userbyid(proowner) FROM pg_proc
 WHERE pronamespace = 'public'::regnamespace
"""

# The history seeded at 0007, read through the columns 0007 has (0011 adds more to transactions).
SEEDED_HISTORY = """
SELECT 'user' AS row_kind, u.id::text AS id, u.email AS detail, NULL::bigint AS amount
  FROM users u
UNION ALL SELECT 'account', a.id::text, a.kind, a.balance_minor FROM accounts a
UNION ALL SELECT 'transaction', t.id::text, t.kind || '/' || t.entry_count, NULL FROM transactions t
UNION ALL SELECT 'entry', e.id::text, e.account_id::text || '#' || e.sequence_number, e.amount_minor
  FROM ledger_entries e
ORDER BY 1, 2
"""


async def _seed_history_at_0007(url: URL) -> None:
    """As the admin, at 0007: a user, their GBP account and a balanced deposit into it (header,
    two entries, both balances), committed through the 0007 triggers."""
    user, account, transaction = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine = _engine(url)
    try:
        async with engine.begin() as connection:
            ledger = await connection.scalar(text("SELECT id FROM ledgers WHERE currency = 'GBP'"))
            settlement = await connection.scalar(
                text("SELECT id FROM accounts WHERE ledger_id = :l AND kind = 'system'"),
                {"l": ledger},
            )
            values = {"u": user, "a": account, "t": transaction, "l": ledger, "s": settlement}
            for statement in (
                "INSERT INTO users (id, email) VALUES (:u, 'seeded-at-0007@example.com')",
                "INSERT INTO accounts (id, ledger_id, user_id, kind) "
                "VALUES (:a, :l, :u, 'customer')",
                "INSERT INTO transactions (id, ledger_id, kind, entry_count) "
                "VALUES (:t, :l, 'deposit', 2)",
                "INSERT INTO ledger_entries (transaction_id, account_id, ledger_id, amount_minor) "
                "VALUES (:t, :a, :l, 750), (:t, :s, :l, -750)",
                "UPDATE accounts SET balance_minor = balance_minor + 750 WHERE id = :a",
                "UPDATE accounts SET balance_minor = balance_minor - 750 WHERE id = :s",
            ):
                await connection.execute(text(statement), values)
    finally:
        await engine.dispose()


async def _assert_runtime_privileges_are_the_matrix(url: URL) -> None:
    """ledger_app holds exactly migration 0013's table and column privileges, nothing granted
    by anyone else, nothing delegable, nothing to PUBLIC (tests.database.RUNTIME_PRIVILEGES)."""
    rows = await _query(
        url,
        "SELECT c.relname AS table_name, a.attname AS column_name, p.privilege, "
        "has_table_privilege(:app, c.oid, p.privilege) AS on_table, "
        "has_column_privilege(:app, c.oid, a.attnum, p.privilege) AS on_column "
        "FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
        "AND NOT a.attisdropped, unnest(ARRAY['SELECT', 'INSERT', 'UPDATE']) p(privilege) "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'",
        app=DEFAULT_ROLE_NAMES.app,
    )
    assert {row.table_name for row in rows} == set(RUNTIME_PRIVILEGES)
    for row in rows:
        expected = RUNTIME_PRIVILEGES[row.table_name]
        allowed_columns = {"INSERT": expected.insert, "UPDATE": expected.update}.get(
            row.privilege, frozenset()
        )
        on_table = row.privilege in expected.table
        assert row.on_table == on_table, (row.table_name, row.privilege)
        assert row.on_column == (on_table or row.column_name in allowed_columns), (
            row.table_name,
            row.column_name,
            row.privilege,
        )
    for privilege in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        granted = await _scalar(
            url,
            "SELECT count(*) FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
            "AND c.relkind = 'r' AND has_table_privilege(:app, c.oid, :p)",
            app=DEFAULT_ROLE_NAMES.app,
            p=privilege,
        )
        assert granted == 0, privilege
    assert await unexpected_runtime_grants(url) == []


async def test_a_0007_volume_bootstrapped_and_upgraded_as_the_development_one_will_be(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The three disposable-cluster conditions of tests.database, checked explicitly here too:
    # this test creates cluster-wide roles with the real names.
    assert os.environ.get(DISPOSABLE_CLUSTER_VARIABLE) == "disposable"
    server = _engine(cluster.server)
    try:
        async with server.connect() as connection:
            marker = await connection.scalar(
                text("SELECT current_setting(:name, true)"), {"name": DISPOSABLE_SERVER_SETTING}
            )
            assert marker == "disposable"
            await assert_disposable_cluster(connection)  # also: not a migrated database
    finally:
        await server.dispose()

    # The development database's state: migrated to 0007 by the admin superuser, with history.
    url = await cluster.database("wiring_from_0007")  # unique; dropped by this test's cleanup
    admin = _engine(url)
    try:
        await run_alembic(admin, lambda config: command.upgrade(config, "0007"))
    finally:
        await admin.dispose()
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == "0007"
    await _seed_history_at_0007(url)
    history = await _query(url, SEEDED_HISTORY)
    # One user; three accounts (the two seeded settlement accounts, the user's); the deposit;
    # its two entries.
    assert len(history) == 1 + 3 + 1 + 2
    before = {(r.kind, r.name): r.owner for r in await _query(url, OWNED_TABLES_AND_ROUTINES)}
    assert set(before) == OBJECTS_AT_0007
    assert set(before.values()) == {url.username}

    # The real bootstrap CLI, the real names: exactly 0007's twelve objects change hands.
    out = await _run_bootstrap_cli(url, monkeypatch, capsys)
    assert f"objects transferred to {DEFAULT_ROLE_NAMES.owner}: {len(OBJECTS_AT_0007)}" in out
    after_bootstrap = {
        (r.kind, r.name): r.owner for r in await _query(url, OWNED_TABLES_AND_ROUTINES)
    }
    assert after_bootstrap == {key: DEFAULT_ROLE_NAMES.owner for key in OBJECTS_AT_0007}
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == "0007"

    # 0008-0013, in one run of the real alembic command line, as ledger_owner.
    await _migrate_as_owner(url)
    await _assert_owned_by_ledger_owner(url)  # 0013, everything (new objects too) the owner's
    # 0008/0009, run by the owner, pinned the search_path of the trigger functions that read
    # tables (as 0010-0012 created theirs). The two that read nothing never needed it (ADR 0012).
    configs = {
        row.proname: row.proconfig
        for row in await _query(
            url,
            "SELECT proname, proconfig FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
            "AND prorettype = 'trigger'::regtype",
        )
    }
    reads_no_table = {"forbid_ledger_mutation", "forbid_audit_mutation"}
    assert len(configs) == 7
    assert {name: configs[name] for name in reads_no_table} == dict.fromkeys(reads_no_table)
    assert all(
        config == ["search_path=pg_catalog, pg_temp"]
        for name, config in configs.items()
        if name not in reads_no_table
    ), configs

    # The history from before survives, row for row.
    assert await _query(url, SEEDED_HISTORY) == history
    await _assert_runtime_privileges_are_the_matrix(url)

    # The API, as ledger_app: guard passes, ready, and a full flow beside the old history.
    user_id = await _use_the_api_as_ledger_app(url)
    await _assert_consistent_and_audited(url, user_id)
