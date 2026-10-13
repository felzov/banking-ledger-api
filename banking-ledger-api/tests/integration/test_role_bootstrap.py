"""The administrative role bootstrap against a real server (Phase 10 E2, admin.bootstrap).

Isolation. PostgreSQL roles are cluster-wide, so every test bootstraps UNIQUE throwaway role
names (lbt_<random>_owner/_app/_runtime), never ledger_owner, ledger_app or ledger_runtime,
and drops them afterwards. Every database a test bootstraps is a disposable one of its own
(the *_test guard of tests.database applies); the shared test database and the development
database are never bootstrapped. Roles are dropped only after their databases, which removes
the grants they held there.
"""

import asyncio
import base64
import hmac
import logging
import re
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from alembic import command
from pydantic import SecretStr
from sqlalchemy import URL, Connection, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_api.admin import bootstrap as bootstrap_module
from ledger_api.admin.bootstrap import (
    SEQUENCE_HELPER,
    BootstrapError,
    BootstrapReport,
    RoleNames,
    bootstrap,
)
from ledger_api.admin.scram import scram_sha256_verifier
from ledger_api.data.errors import sqlstate
from tests.database import (
    assert_disposable,
    configured_database_url,
    disposable_database_url,
    recreate_database,
    run_alembic,
)

OWNER_PASSWORD = SecretStr("synthetic-owner-password-0001")
APP_PASSWORD = SecretStr("synthetic-app-password-000001")
HEAD = "0012"
PINNED_SEARCH_PATH = ["search_path=pg_catalog, pg_temp"]
VERIFIER = re.compile(r"SCRAM-SHA-256\$(\d+):([A-Za-z0-9+/=]+)\$")


# --- isolation ----------------------------------------------------------------------------------


@dataclass
class Cluster:
    """Disposable databases and unique roles for one test, removed afterwards."""

    server: URL
    databases: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)

    async def database(self, name: str) -> URL:
        url = disposable_database_url(self.server, f"_bootstrap_{name}")
        assert url.database is not None
        await recreate_database(self.server, url.database)
        self.databases.append(url.database)
        return url

    def role_names(self) -> RoleNames:
        token = secrets.token_hex(5)
        names = RoleNames(f"lbt_{token}_owner", f"lbt_{token}_app", f"lbt_{token}_runtime")
        self.roles.extend(names.all())
        return names

    def role(self) -> str:
        name = f"lbt_{secrets.token_hex(5)}_other"
        self.roles.append(name)
        return name

    async def cleanup(self) -> None:
        engine = create_async_engine(self.server, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                for database in self.databases:
                    assert_disposable(database)
                    quoted = connection.dialect.identifier_preparer.quote(database)
                    await connection.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
                for role in self.roles:
                    assert role.startswith("lbt_")  # never a role this suite did not create
                    await connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        finally:
            await engine.dispose()


@pytest.fixture
async def cluster() -> AsyncIterator[Cluster]:
    cluster = Cluster(configured_database_url())
    try:
        yield cluster
    finally:
        await cluster.cleanup()


# --- helpers ------------------------------------------------------------------------------------


def _engine(url: URL) -> AsyncEngine:
    return create_async_engine(url, poolclass=NullPool)


def _as(url: URL, role: str, password: SecretStr) -> URL:
    return url.set(username=role, password=password.get_secret_value())


async def _bootstrap(
    url: URL,
    names: RoleNames,
    *,
    owner_password: SecretStr = OWNER_PASSWORD,
    app_password: SecretStr = APP_PASSWORD,
    on_engine: Callable[[AsyncEngine], None] | None = None,
) -> BootstrapReport:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    if on_engine is not None:
        on_engine(engine)
    try:
        async with engine.connect() as connection:
            return await bootstrap(
                connection, names, owner_password=owner_password, app_password=app_password
            )
    finally:
        await engine.dispose()


async def _migrate(url: URL, revision: str = "head", *, down: bool = False) -> None:
    engine = _engine(url)
    try:
        operation = command.downgrade if down else command.upgrade
        await run_alembic(engine, lambda config: operation(config, revision))
    finally:
        await engine.dispose()


async def _query(url: URL, sql: str, **params: Any) -> list[Any]:
    engine = _engine(url)
    try:
        async with engine.connect() as connection:
            return list((await connection.execute(text(sql), params)).all())
    finally:
        await engine.dispose()


async def _scalar(url: URL, sql: str, **params: Any) -> Any:
    rows = await _query(url, sql, **params)
    return rows[0][0] if rows else None


async def _roles_exist(url: URL, names: tuple[str, ...]) -> set[str]:
    rows = await _query(url, "SELECT rolname FROM pg_roles WHERE rolname = ANY(:n)", n=list(names))
    return {row.rolname for row in rows}


async def _owners_in_public(url: URL) -> set[str]:
    """Owners of every relation (all kinds), routine and type in public."""
    rows = await _query(
        url,
        "SELECT pg_get_userbyid(relowner) FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace "
        "UNION SELECT pg_get_userbyid(proowner) FROM pg_proc "
        "WHERE pronamespace = 'public'::regnamespace "
        "UNION SELECT pg_get_userbyid(typowner) FROM pg_type "
        "WHERE typnamespace = 'public'::regnamespace",
    )
    return {row[0] for row in rows}


async def _helper(url: URL) -> Any:
    rows = await _query(
        url,
        "SELECT pg_get_userbyid(p.proowner) AS owner, pg_get_userbyid(c.relowner) AS table_owner, "
        "p.prosecdef, p.proconfig, p.proacl::text AS acl "
        "FROM pg_proc p, pg_class c "
        "WHERE p.oid = to_regprocedure(:helper) AND c.oid = 'public.ledger_entries'::regclass",
        helper=SEQUENCE_HELPER,
    )
    return rows[0]


def _verifies(verifier: str, password: SecretStr) -> bool:
    """Whether a stored SCRAM verifier was derived from `password` (same salt, iterations)."""
    match = VERIFIER.match(verifier)
    assert match is not None, "not a SCRAM-SHA-256 verifier"
    salt, iterations = base64.b64decode(match[2]), int(match[1])
    expected = scram_sha256_verifier(password.get_secret_value(), salt=salt, iterations=iterations)
    return hmac.compare_digest(expected, verifier)


async def _stored_verifier(url: URL, role: str) -> str | None:
    value: str | None = await _scalar(
        url, "SELECT rolpassword FROM pg_authid WHERE rolname = :r", r=role
    )
    return value


async def _expect_refused(connection: AsyncConnection, statement: str) -> None:
    """The statement fails with insufficient_privilege (42501) and nothing else."""
    async with connection.begin():
        with pytest.raises(DBAPIError) as excinfo:
            await connection.execute(text(statement))
    assert sqlstate(excinfo.value) == "42501", excinfo.value


# --- first and repeated bootstrap ---------------------------------------------------------------


async def test_first_bootstrap_creates_restricted_roles(cluster: Cluster) -> None:
    url = await cluster.database("first")
    names = cluster.role_names()

    report = await _bootstrap(url, names)

    assert report.database == url.database
    assert report.created_roles == (names.runtime_group, names.owner, names.app)
    assert report.transferred_objects == ()  # an empty database has nothing to transfer

    roles = {
        row.rolname: row
        for row in await _query(
            url, "SELECT * FROM pg_roles WHERE rolname = ANY(:n)", n=list(names.all())
        )
    }
    for row in roles.values():
        assert not row.rolsuper
        assert not row.rolcreatedb
        assert not row.rolcreaterole
        assert not row.rolreplication
        assert not row.rolbypassrls
    assert (roles[names.owner].rolcanlogin, roles[names.owner].rolinherit) == (True, False)
    assert (roles[names.app].rolcanlogin, roles[names.app].rolinherit) == (True, True)
    assert (roles[names.runtime_group].rolcanlogin, roles[names.runtime_group].rolinherit) == (
        False,
        False,
    )

    memberships = await _query(
        url,
        "SELECT g.rolname AS role, m.rolname AS member, inherit_option, set_option, admin_option "
        "FROM pg_auth_members am JOIN pg_roles g ON g.oid = am.roleid "
        "JOIN pg_roles m ON m.oid = am.member "
        "WHERE g.rolname = ANY(:n) OR m.rolname = ANY(:n)",
        n=list(names.all()),
    )
    assert [tuple(row) for row in memberships] == [
        (names.runtime_group, names.app, True, False, False)
    ]

    # The database: only the two login roles may connect; nobody gets TEMPORARY or CREATE.
    database_acl = await _query(
        url,
        "SELECT CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, "
        "a.privilege_type FROM pg_database d, aclexplode(d.datacl) a "
        "WHERE d.datname = current_database() AND a.grantee <> d.datdba",
    )
    assert sorted(tuple(row) for row in database_acl) == sorted(
        [(names.owner, "CONNECT"), (names.app, "CONNECT")]
    )

    # The public schema: the owner may create, the runtime group may only use it.
    schema_acl = await _query(
        url,
        "SELECT CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, "
        "a.privilege_type FROM pg_namespace n, aclexplode(n.nspacl) a "
        "WHERE n.nspname = 'public' AND a.grantee <> n.nspowner",
    )
    assert sorted(tuple(row) for row in schema_acl) == sorted(
        [(names.owner, "CREATE"), (names.owner, "USAGE"), (names.runtime_group, "USAGE")]
    )


async def test_repeated_bootstrap_converges_and_rotates_passwords(cluster: Cluster) -> None:
    url = await cluster.database("repeat")
    names = cluster.role_names()
    await _bootstrap(url, names)

    # Drift a bootstrap must undo: attributes, membership options, database privileges.
    engine = _engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'ALTER ROLE "{names.app}" CREATEDB NOINHERIT'))
            await connection.execute(text(f'ALTER ROLE "{names.owner}" CONNECTION LIMIT 1'))
            await connection.execute(
                text(f'GRANT "{names.runtime_group}" TO "{names.app}" WITH SET TRUE')
            )
            await connection.execute(
                text(f'GRANT TEMPORARY ON DATABASE "{url.database}" TO "{names.app}"')
            )
            await connection.execute(text(f'GRANT CREATE ON SCHEMA public TO "{names.app}"'))
    finally:
        await engine.dispose()

    new_app_password = SecretStr("synthetic-app-password-000002")
    report = await _bootstrap(url, names, app_password=new_app_password)

    assert report.created_roles == ()
    app = (await _query(url, "SELECT * FROM pg_roles WHERE rolname = :r", r=names.app))[0]
    owner = (await _query(url, "SELECT * FROM pg_roles WHERE rolname = :r", r=names.owner))[0]
    assert (app.rolcreatedb, app.rolinherit, owner.rolconnlimit) == (False, True, -1)
    assert (
        await _scalar(
            url,
            "SELECT set_option FROM pg_auth_members WHERE roleid = to_regrole(:g)",
            g=names.runtime_group,
        )
        is False
    )
    privileges = await _query(
        url,
        "SELECT has_database_privilege(:r, current_database(), 'TEMPORARY'), "
        "has_schema_privilege(:r, 'public', 'CREATE')",
        r=names.app,
    )
    assert tuple(privileges[0]) == (False, False)

    app_verifier = await _stored_verifier(url, names.app)
    assert app_verifier is not None
    assert _verifies(app_verifier, new_app_password)
    assert not _verifies(app_verifier, APP_PASSWORD)


async def test_concurrent_bootstraps_of_one_database_wait_for_each_other(
    cluster: Cluster,
) -> None:
    url = await cluster.database("concurrent")
    names = cluster.role_names()
    database_lock_waits = (
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )
    holder_engine = _engine(url)
    try:
        async with holder_engine.connect() as holder:
            # Hold the bootstrap's own lock, so that both runs are certain to overlap.
            transaction = await holder.begin()
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": bootstrap_module.ADVISORY_LOCK_KEY},
            )
            runs = [asyncio.create_task(_bootstrap(url, names)) for _ in range(2)]
            waiting = 0
            for _ in range(100):  # 5 s at most, well within the bootstrap's lock_timeout
                waiting = await _scalar(url, database_lock_waits)
                if waiting == 2:
                    break
                await asyncio.sleep(0.05)
            await transaction.commit()
            first, second = await asyncio.gather(*runs)
    finally:
        await holder_engine.dispose()

    assert waiting == 2  # both runs queued on the advisory lock
    # One of them created every role; the other, once the lock was free, found them.
    assert sorted(first.created_roles + second.created_roles) == sorted(names.all())


# --- ownership: existing and fresh volumes ------------------------------------------------------


async def test_existing_volume_moves_every_object_to_the_owner(cluster: Cluster) -> None:
    url = await cluster.database("existing")
    names = cluster.role_names()
    await _migrate(url)  # today's world: migrated, everything owned by the admin superuser
    admin = url.username
    before = await _helper(url)
    assert before.owner == before.table_owner == admin

    report = await _bootstrap(url, names)

    assert await _owners_in_public(url) == {names.owner}
    # Tables and routines are listed; what follows a table (indexes, identity sequences) is not.
    assert "table public.ledger_entries" in report.transferred_objects
    assert "table public.alembic_version" in report.transferred_objects
    assert f"function {SEQUENCE_HELPER}" in report.transferred_objects
    assert not [o for o in report.transferred_objects if o.startswith(("sequence", "index"))]
    identity_owner = await _scalar(
        url,
        "SELECT pg_get_userbyid(relowner) FROM pg_class "
        "WHERE oid = pg_get_serial_sequence('public.ledger_entries', 'sequence_number')::regclass",
    )
    assert identity_owner == names.owner

    # The helper still runs as the table owner, pinned, and PUBLIC still cannot execute it.
    helper = await _helper(url)
    assert helper.owner == helper.table_owner == names.owner
    assert helper.prosecdef is True
    assert helper.proconfig == PINNED_SEARCH_PATH
    assert helper.acl == f"{{{names.owner}=X/{names.owner}}}"
    # The trigger functions keep their pinned search_path (0008, 0009).
    configs = await _query(
        url,
        "SELECT proname, proconfig FROM pg_proc WHERE proname = ANY(:p)",
        p=["assert_entry_sequence_order", "assert_transaction_balanced"],
    )
    assert {row.proname: row.proconfig for row in configs} == {
        "assert_entry_sequence_order": PINNED_SEARCH_PATH,
        "assert_transaction_balanced": PINNED_SEARCH_PATH,
    }
    # Data is untouched: the two seeded ledgers.
    assert await _scalar(url, "SELECT count(*) FROM ledgers") == 2

    # The owner, not a superuser, can now run migrations on the transferred objects.
    as_owner = _as(url, names.owner, OWNER_PASSWORD)
    await _migrate(as_owner, "-1", down=True)
    await _migrate(as_owner)
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD
    assert await _owners_in_public(url) == {names.owner}


async def test_fresh_volume_is_migrated_by_the_owner_without_a_superuser(
    cluster: Cluster,
) -> None:
    url = await cluster.database("fresh")
    names = cluster.role_names()
    await _bootstrap(url, names)
    as_owner = _as(url, names.owner, OWNER_PASSWORD)

    assert (
        await _scalar(as_owner, "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        is False
    )
    await _migrate(as_owner)

    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD
    assert await _owners_in_public(url) == {names.owner}
    helper = await _helper(url)
    assert helper.owner == helper.table_owner == names.owner
    assert helper.acl == f"{{{names.owner}=X/{names.owner}}}"
    # A second bootstrap of the migrated database finds nothing to transfer, and passes its
    # checks of the helper.
    assert (await _bootstrap(url, names)).transferred_objects == ()


async def test_runtime_role_has_nothing_before_migration_0013(cluster: Cluster) -> None:
    url = await cluster.database("runtime")
    names = cluster.role_names()
    await _bootstrap(url, names)
    await _migrate(_as(url, names.owner, OWNER_PASSWORD))

    engine = _engine(_as(url, names.app, APP_PASSWORD))
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_user")) == names.app
            await connection.commit()
            await _expect_refused(connection, f'SET ROLE "{names.runtime_group}"')
            await _expect_refused(connection, f'SET ROLE "{names.owner}"')
            await _expect_refused(connection, "CREATE TABLE public.intruder (id int)")
            await _expect_refused(connection, "CREATE TEMPORARY TABLE accounts (id int)")
            # No table privileges yet: those are migration 0013's.
            await _expect_refused(connection, "SELECT 1 FROM public.users")
            await _expect_refused(connection, "SELECT 1 FROM public.ledger_entries")
            await _expect_refused(connection, "SET session_replication_role = replica")
    finally:
        await engine.dispose()
    owned = await _scalar(
        url,
        "SELECT count(*) FROM pg_class WHERE relowner = to_regrole(:r)",
        r=names.app,
    )
    assert owned == 0


async def test_login_roles_cannot_connect_to_another_bootstrapped_database(
    cluster: Cluster,
) -> None:
    first_url, second_url = await cluster.database("connect_a"), await cluster.database("connect_b")
    first, second = cluster.role_names(), cluster.role_names()
    await _bootstrap(first_url, first)
    await _bootstrap(second_url, second)

    for role, password in ((first.app, APP_PASSWORD), (first.owner, OWNER_PASSWORD)):
        engine = _engine(_as(second_url, role, password))
        try:
            with pytest.raises(DBAPIError) as excinfo:
                async with engine.connect():
                    pass
        finally:
            await engine.dispose()
        assert sqlstate(excinfo.value) == "42501"  # no CONNECT on that database


# --- scope and refusals -------------------------------------------------------------------------


async def _database_state(url: URL) -> list[Any]:
    """ACLs and owners an unrelated bootstrap must not change."""
    return await _query(
        url,
        "SELECT 'database', datacl::text, pg_get_userbyid(datdba) FROM pg_database "
        "WHERE datname = current_database() "
        "UNION ALL SELECT 'schema', nspacl::text, pg_get_userbyid(nspowner) FROM pg_namespace "
        "WHERE nspname = 'public' "
        "UNION ALL SELECT relname::text, relacl::text, pg_get_userbyid(relowner) FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace ORDER BY 1",
    )


async def test_bootstrap_leaves_other_databases_and_roles_alone(cluster: Cluster) -> None:
    target, bystander = await cluster.database("scope_a"), await cluster.database("scope_b")
    await _migrate(bystander)
    unrelated = cluster.role()
    engine = _engine(bystander)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE ROLE "{unrelated}" NOLOGIN CREATEDB'))
            await connection.execute(text(f'GRANT SELECT ON public.ledgers TO "{unrelated}"'))
    finally:
        await engine.dispose()
    bystander_before = await _database_state(bystander)
    role_query = "SELECT rolcreatedb, rolcanlogin FROM pg_roles WHERE rolname = :r"
    unrelated_before = await _query(bystander, role_query, r=unrelated)

    await _bootstrap(target, cluster.role_names())

    assert await _database_state(bystander) == bystander_before
    assert await _query(bystander, role_query, r=unrelated) == unrelated_before


async def test_refuses_to_configure_the_admin_itself(cluster: Cluster) -> None:
    url = await cluster.database("self")
    names = cluster.role_names()
    assert url.username is not None
    as_admin = RoleNames(owner=url.username, app=names.app, runtime_group=names.runtime_group)

    with pytest.raises(BootstrapError, match="runs the bootstrap"):
        await _bootstrap(url, as_admin)

    assert await _roles_exist(url, names.all()) == set()


async def test_refuses_an_existing_superuser_and_changes_nothing(cluster: Cluster) -> None:
    url = await cluster.database("superuser")
    names = cluster.role_names()
    await _query_commit(url, f'CREATE ROLE "{names.app}" SUPERUSER NOLOGIN')

    with pytest.raises(BootstrapError, match="refusing to reuse superuser role"):
        await _bootstrap(url, names)

    assert await _scalar(url, "SELECT rolsuper FROM pg_roles WHERE rolname = :r", r=names.app)
    assert await _roles_exist(url, names.all()) == {names.app}


async def test_refuses_unexpected_memberships(cluster: Cluster) -> None:
    url = await cluster.database("membership")
    names = cluster.role_names()
    await _query_commit(
        url, f'CREATE ROLE "{names.app}" LOGIN', f'GRANT pg_read_all_data TO "{names.app}"'
    )

    with pytest.raises(BootstrapError, match=f"{names.app} in pg_read_all_data"):
        await _bootstrap(url, names)

    assert await _roles_exist(url, names.all()) == {names.app}


async def test_refuses_foreign_owned_objects_and_rolls_everything_back(cluster: Cluster) -> None:
    url = await cluster.database("foreign")
    names = cluster.role_names()
    stranger = cluster.role()
    await _query_commit(
        url,
        f'CREATE ROLE "{stranger}" NOLOGIN',
        "CREATE TABLE public.stray (id int)",
        f'ALTER TABLE public.stray OWNER TO "{stranger}"',
    )
    before = await _database_state(url)

    with pytest.raises(BootstrapError, match=rf"table public\.stray \(owner {stranger}\)"):
        await _bootstrap(url, names)

    # The roles were created and the database restricted earlier in the same transaction: all
    # of it was rolled back.
    assert await _roles_exist(url, names.all()) == set()
    assert await _database_state(url) == before


async def test_refuses_an_admin_that_is_not_a_superuser(cluster: Cluster) -> None:
    url = await cluster.database("not_admin")
    first, second = cluster.role_names(), cluster.role_names()
    await _bootstrap(url, first)

    with pytest.raises(BootstrapError, match="is not a superuser"):
        await _bootstrap(_as(url, first.owner, OWNER_PASSWORD), second)

    assert await _roles_exist(url, second.all()) == set()


async def test_refuses_a_connection_already_in_a_transaction(cluster: Cluster) -> None:
    url = await cluster.database("in_transaction")
    engine = _engine(url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))  # autobegins
            with pytest.raises(BootstrapError, match="its own transaction"):
                await bootstrap(
                    connection,
                    cluster.role_names(),
                    owner_password=OWNER_PASSWORD,
                    app_password=APP_PASSWORD,
                )
    finally:
        await engine.dispose()


async def _query_commit(url: URL, *statements: str) -> None:
    engine = _engine(url)
    try:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


# --- passwords ----------------------------------------------------------------------------------


async def test_verifier_matches_the_one_postgresql_computes(cluster: Cluster) -> None:
    url = await cluster.database("scram")
    probe = cluster.role()
    password = SecretStr("synthetic-probe-password-0001")
    engine = _engine(url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                # PostgreSQL hashes this plaintext itself (a synthetic value, rolled back).
                await connection.execute(text("SET LOCAL password_encryption = 'scram-sha-256'"))
                await connection.execute(text(f'CREATE ROLE "{probe}" NOLOGIN'))
                await connection.execute(
                    text(f"ALTER ROLE \"{probe}\" PASSWORD '{password.get_secret_value()}'")
                )
                theirs = await connection.scalar(
                    text("SELECT rolpassword FROM pg_authid WHERE rolname = :r"), {"r": probe}
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()

    assert isinstance(theirs, str)
    match = VERIFIER.match(theirs)
    assert match is not None
    ours = scram_sha256_verifier(
        password.get_secret_value(), salt=base64.b64decode(match[2]), iterations=int(match[1])
    )
    assert ours == theirs


async def test_stored_passwords_are_verifiers_of_the_configured_passwords(
    cluster: Cluster,
) -> None:
    url = await cluster.database("stored")
    names = cluster.role_names()
    await _bootstrap(url, names)

    owner_verifier = await _stored_verifier(url, names.owner)
    app_verifier = await _stored_verifier(url, names.app)
    assert owner_verifier is not None
    assert _verifies(owner_verifier, OWNER_PASSWORD)
    assert app_verifier is not None
    assert _verifies(app_verifier, APP_PASSWORD)
    assert await _stored_verifier(url, names.runtime_group) is None


async def test_no_plaintext_password_is_ever_sent_or_logged(
    cluster: Cluster, caplog: pytest.LogCaptureFixture
) -> None:
    url = await cluster.database("plaintext")
    names = cluster.role_names()
    statements: list[str] = []
    parameters: list[str] = []

    def record(engine: AsyncEngine) -> None:
        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def capture(conn: Connection, cursor: Any, statement: str, params: Any, *args: Any) -> None:
            statements.append(statement)
            parameters.append(repr(params))

    # SQLAlchemy sets its own "sqlalchemy" logger to WARNING at import, so a root level is not
    # enough: enable statement logging (INFO) on the engine logger itself.
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")
    await _bootstrap(url, names, on_engine=record)

    verifiers = [await _stored_verifier(url, role) for role in (names.owner, names.app)]
    assert all(verifiers)
    sent_text, sent_values = "\n".join(statements), "\n".join(parameters)
    # Non-vacuous: SQL logging ran, and the listener saw the verifiers travel as bound values.
    assert "set_config" in caplog.text
    for verifier in verifiers:
        assert verifier is not None
        assert verifier in sent_values
        # ...and nowhere else: no statement text, no log line (hide_parameters).
        assert verifier not in sent_text
        assert verifier not in caplog.text
    for password in (OWNER_PASSWORD, APP_PASSWORD):
        for haystack in (sent_text, sent_values, caplog.text):
            assert password.get_secret_value() not in haystack


async def test_a_failed_password_statement_quotes_neither_password_nor_verifier(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = await cluster.database("password_failure")
    names = cluster.role_names()
    # A real server error whose own MESSAGE quotes the verifier: the worst case to report.
    leaking = text(
        "DO $$ BEGIN RAISE EXCEPTION 'cannot use %', "
        "pg_catalog.current_setting('ledger_bootstrap.verifier'); END $$"
    )
    monkeypatch.setattr(bootstrap_module, "SET_PASSWORD", leaking)

    with pytest.raises(BootstrapError) as excinfo:
        await _bootstrap(url, names)

    error = excinfo.value
    assert str(error) == f"setting the password of role {names.owner} failed (SQLSTATE P0001)"
    assert error.__cause__ is None
    assert error.__context__ is None  # the database error (verifier in its parameters) is gone
    assert await _roles_exist(url, names.all()) == set()  # rolled back


# --- F1: name resolution cannot be hijacked through public -------------------------------------

# Operators the bootstrap's catalog queries use where pg_catalog has no exact match (it relies on
# an implicit coercion), so a same-named operator in public with these exact types would win.
HIJACKED_OPERATORS = [
    ("=", "regnamespace"),
    ("=", "regclass"),
    ("=", "regrole"),
    ("=", "regprocedure"),
    ("=", "integer"),
    ("<>", "regrole"),
]


async def _plant_hijacking_operators(as_owner: URL) -> None:
    """As the owner role (CREATE on public, no superuser): operators that record who called
    them, then answer like the real ones."""
    statements = ["CREATE TABLE public.hijack_marker (caller text, operator text)"]
    for number, (operator, right) in enumerate(HIJACKED_OPERATORS):
        negate = "NOT " if operator == "<>" else ""
        statements += [
            # Test-controlled literals only (HIJACKED_OPERATORS).
            f"CREATE FUNCTION public.hijack_{number}(l oid, r {right}) RETURNS boolean "  # noqa: S608
            "LANGUAGE plpgsql AS $$ BEGIN "
            "INSERT INTO public.hijack_marker VALUES "
            f"(current_user, 'oid {operator} {right}'); "
            f"RETURN {negate}(l OPERATOR(pg_catalog.=) r::oid); END $$",
            f"CREATE OPERATOR public.{operator} (LEFTARG = oid, RIGHTARG = {right}, "
            f"FUNCTION = public.hijack_{number})",
        ]
    await _query_commit(as_owner, *statements)


async def test_public_operators_cannot_hijack_the_bootstrap(cluster: Cluster) -> None:
    url = await cluster.database("hijack")
    names = cluster.role_names()
    await _bootstrap(url, names)  # gives the owner role CREATE on public
    await _plant_hijacking_operators(_as(url, names.owner, OWNER_PASSWORD))

    # Control: with the default search_path, a superuser's catalog query runs the planted
    # operator (and so would run anything in it). Rolled back.
    engine = _engine(url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "SELECT count(*) FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace"
                )
            )
            hijacked = await connection.scalar(text("SELECT count(*) FROM public.hijack_marker"))
            await transaction.rollback()
    finally:
        await engine.dispose()
    assert hijacked > 0

    # The bootstrap, through a plain connection (no session-level search_path), runs every one of
    # those queries again, as a superuser: none of the planted operators is ever called.
    await _bootstrap(url, names)

    assert await _scalar(url, "SELECT count(*) FROM public.hijack_marker") == 0


async def test_the_commands_engine_pins_search_path_for_the_whole_session(
    cluster: Cluster,
) -> None:
    url = await cluster.database("session_path")
    engine = bootstrap_module.create_admin_engine(url.render_as_string(hide_password=False))
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SHOW search_path")) == "pg_catalog, pg_temp"
    finally:
        await engine.dispose()
    assert engine.sync_engine.hide_parameters


# --- F2: privileges granted by another grantor ---------------------------------------------------


@pytest.mark.parametrize(
    ("delegation", "grant", "expected"),
    [
        (
            'GRANT TEMPORARY ON DATABASE "{database}" TO "{delegate}" WITH GRANT OPTION',
            'GRANT TEMPORARY ON DATABASE "{database}" TO PUBLIC',
            r"TEMPORARY on database to PUBLIC \(granted by {delegate}\)",
        ),
        (
            'GRANT CREATE ON SCHEMA public TO "{delegate}" WITH GRANT OPTION',
            "GRANT CREATE ON SCHEMA public TO PUBLIC",
            r"CREATE on schema public to PUBLIC \(granted by {delegate}\)",
        ),
        (
            'GRANT TEMPORARY ON DATABASE "{database}" TO "{delegate}" WITH GRANT OPTION',
            'GRANT TEMPORARY ON DATABASE "{database}" TO "{app}"',
            r"TEMPORARY on database to {app} \(granted by {delegate}\)",
        ),
    ],
    ids=["database-public", "schema-public", "database-app"],
)
async def test_refuses_privileges_another_grantor_gave(
    cluster: Cluster, delegation: str, grant: str, expected: str
) -> None:
    url = await cluster.database("grantor")
    names = cluster.role_names()
    delegate = cluster.role()
    values = {"database": url.database, "delegate": delegate, "app": names.app}
    await _query_commit(
        url,
        f'CREATE ROLE "{delegate}" NOLOGIN',
        f'CREATE ROLE "{names.app}" NOLOGIN',
        delegation.format(**values),
        # A grant made BY the delegate: the bootstrap's REVOKEs (as the owner) cannot reach it.
        f'SET ROLE "{delegate}"',
        grant.format(**values),
        "RESET ROLE",
    )
    before = await _database_state(url)

    with pytest.raises(BootstrapError, match=expected.format(**values)):
        await _bootstrap(url, names)

    assert await _database_state(url) == before  # rolled back
    assert await _roles_exist(url, names.all()) == {names.app}


# --- F3: what the target roles may own, and default privileges ---------------------------------


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ('ALTER DATABASE "{database}" OWNER TO "{app}"', r"{app} owns database {database}"),
        ('CREATE SCHEMA app_space AUTHORIZATION "{app}"', r"{app} owns schema app_space"),
        (
            'CREATE TABLE public.app_table (id int); ALTER TABLE public.app_table OWNER TO "{app}"',
            r"{app} owns table (public\.)?app_table",
        ),
        ('ALTER DATABASE "{database}" OWNER TO "{owner}"', r"{owner} owns database {database}"),
    ],
    ids=["app-owns-database", "app-owns-schema", "app-owns-table", "owner-owns-database"],
)
async def test_refuses_target_roles_owning_what_they_must_not(
    cluster: Cluster, setup: str, expected: str
) -> None:
    url = await cluster.database("ownerships")
    names = cluster.role_names()
    values = {"database": url.database, "app": names.app, "owner": names.owner}
    await _query_commit(
        url,
        f'CREATE ROLE "{names.app}" NOLOGIN',
        f'CREATE ROLE "{names.owner}" NOLOGIN',
        *setup.format(**values).split("; "),
    )

    with pytest.raises(BootstrapError, match=expected.format(**values)):
        await _bootstrap(url, names)

    assert await _roles_exist(url, names.all()) == {names.app, names.owner}


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        (
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" GRANT SELECT ON TABLES TO "{group}"',
            r"objects of {owner} in every schema granted to {group}",
        ),
        (
            'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA public '
            "GRANT INSERT ON TABLES TO PUBLIC",
            r"objects of {owner} in schema public granted to PUBLIC",
        ),
    ],
    ids=["owner-to-runtime-group", "owner-to-public"],
)
async def test_refuses_default_privileges_that_would_grant_future_objects(
    cluster: Cluster, default: str, expected: str
) -> None:
    url = await cluster.database("defaults")
    names = cluster.role_names()
    await _bootstrap(url, names)
    values = {"owner": names.owner, "group": names.runtime_group}
    await _query_commit(url, default.format(**values))

    with pytest.raises(BootstrapError, match=expected.format(**values)):
        await _bootstrap(url, names)


async def test_accepts_default_privileges_that_only_revoke(cluster: Cluster) -> None:
    url = await cluster.database("revoking_defaults")
    names = cluster.role_names()
    await _bootstrap(url, names)
    await _query_commit(
        url,
        f'ALTER DEFAULT PRIVILEGES FOR ROLE "{names.owner}" '
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
    )

    await _bootstrap(url, names)  # hardening, not a grant: accepted


# --- F4: a failed password change never carries the verifier ----------------------------------


async def _password_change_error(connection: AsyncConnection, block: Any) -> dict[str, Any]:
    """Every field of the server's error for `block`, run for a role that does not exist."""
    await connection.execute(
        text(
            "SELECT set_config('ledger_bootstrap.role', 'lbt_no_such_role', true), "
            "set_config('ledger_bootstrap.verifier', :verifier, true)"
        ),
        {"verifier": FAILING_VERIFIER},
    )
    with pytest.raises(DBAPIError) as excinfo:
        await connection.execute(block)
    cause: Any = excinfo.value.orig.__cause__ if excinfo.value.orig is not None else None
    assert cause is not None  # asyncpg's PostgresError: every field the server sent
    fields: dict[str, Any] = cause.as_dict()
    return fields


FAILING_VERIFIER = scram_sha256_verifier("synthetic-failing-password-01")


async def test_a_failed_alter_role_keeps_the_verifier_out_of_the_error(cluster: Cluster) -> None:
    url = await cluster.database("password_context")
    unhandled = text(
        "DO $$ BEGIN EXECUTE pg_catalog.format('ALTER ROLE %I WITH PASSWORD %L', "
        "pg_catalog.current_setting('ledger_bootstrap.role'), "
        "pg_catalog.current_setting('ledger_bootstrap.verifier')); END $$"
    )
    engine = _engine(url)
    try:
        async with engine.connect() as connection:
            # Control: without the handler, the error's context quotes the whole ALTER ROLE
            # statement, verifier included; that context also goes to the server log.
            async with connection.begin():
                leaked = await _password_change_error(connection, unhandled)
            async with connection.begin():
                fields = await _password_change_error(connection, bootstrap_module.SET_PASSWORD)
    finally:
        await engine.dispose()

    assert FAILING_VERIFIER in repr(leaked)
    assert FAILING_VERIFIER not in repr(fields)
    assert fields["message"] == "setting a role password failed"
    assert fields["sqlstate"] == "42704"  # undefined_object, kept from the real failure


# --- F8: template databases --------------------------------------------------------------------


async def test_refuses_a_template_database(cluster: Cluster) -> None:
    url = await cluster.database("template")
    names = cluster.role_names()
    await _query_commit(url, f'ALTER DATABASE "{url.database}" IS_TEMPLATE true')
    try:
        with pytest.raises(BootstrapError, match="is a template database"):
            await _bootstrap(url, names)
        assert await _roles_exist(url, names.all()) == set()
    finally:
        # A template database cannot be dropped: unmark it for the cleanup.
        await _query_commit(url, f'ALTER DATABASE "{url.database}" IS_TEMPLATE false')
