"""PostgreSQL helpers for the test suite: disposable databases, migrations, violation checks."""

import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import URL, Connection, make_url, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_api.data.errors import violated_constraint
from ledger_api.data.models import AuditEvent
from ledger_api.data.reconciliation import (
    find_balance_mismatches,
    find_invalid_transactions,
    find_ledgers_with_nonzero_balances,
    find_misplaced_entries,
    find_unbalanced_ledgers,
)
from ledger_api.domain.audit import POSTING_ACTIONS

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

# Only databases whose name ends with this suffix may ever be dropped by the test suite.
DISPOSABLE_DATABASE_SUFFIX = "_test"

# The cluster-wide group migration 0013 grants the application's privileges to. Roles belong to
# the whole cluster, so the suite creates this one only on a cluster declared disposable.
RUNTIME_GROUP = "ledger_runtime"

# A disposable cluster is recognised by two markers set independently, both required:
# - the client's environment (LEDGER_TEST_CLUSTER): this process means to test a throwaway server;
# - the server's own configuration (the custom setting ledger.test_cluster): this server is one.
#   Set out of band by whatever owns the throwaway server, addressed by name, never through
#   DATABASE_URL: `make test` by `docker compose exec test-db`, CI by `docker exec` on its service
#   container. A development server never has it, so a DATABASE_URL pointing at one is refused
#   whatever the environment claims.
DISPOSABLE_CLUSTER_VARIABLE = "LEDGER_TEST_CLUSTER"
DISPOSABLE_CLUSTER_VALUE = "disposable"
DISPOSABLE_SERVER_SETTING = "ledger.test_cluster"
DISPOSABLE_SERVER_VALUE = "disposable"


def configured_database_url() -> URL:
    """The DATABASE_URL tests derive their own databases from. Missing means fail, not skip."""
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        pytest.fail(
            "DATABASE_URL is not set. Start PostgreSQL with `make up` and run `make test`.",
            pytrace=False,
        )
    return make_url(raw_url)


def disposable_database_url(server_url: URL, name: str) -> URL:
    """URL of a throwaway database on the same server, e.g. ledger -> ledger_test."""
    return server_url.set(database=f"{server_url.database}{name}{DISPOSABLE_DATABASE_SUFFIX}")


def assert_disposable(database: str) -> None:
    """Mandatory guard before any DROP DATABASE: never touch a development database."""
    if not database.endswith(DISPOSABLE_DATABASE_SUFFIX):
        raise RuntimeError(
            f"refusing to drop database {database!r}: "
            f"only names ending in {DISPOSABLE_DATABASE_SUFFIX!r} are disposable"
        )


async def recreate_database(server_url: URL, database: str) -> None:
    """Drop and create a disposable database, on a cluster that can host the schema: every test
    database is migrated, and migration 0013 grants to the cluster-wide runtime group."""
    assert_disposable(database)
    # CREATE/DROP DATABASE cannot run inside a transaction block, hence AUTOCOMMIT.
    engine = create_async_engine(server_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await assert_disposable_cluster(connection)
            await ensure_runtime_group(connection)
            quoted = connection.dialect.identifier_preparer.quote(database)
            await connection.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
            await connection.execute(text(f"CREATE DATABASE {quoted}"))
    finally:
        await engine.dispose()


async def assert_disposable_cluster(connection: AsyncConnection) -> None:
    """Mandatory guard before the suite creates a database or a role: fail unless `connection`
    is to a disposable test cluster.

    Three independent checks, all required, none sufficient alone:
    1. the environment declares the intent (an environment variable proves nothing by itself:
       it says nothing about which server DATABASE_URL reaches);
    2. the server the connection actually reached carries the disposable marker in its own
       configuration, set out of band by whatever owns it (see DISPOSABLE_SERVER_SETTING);
    3. the database the suite connects to holds no application schema, which a development
       database would.
    """
    if os.environ.get(DISPOSABLE_CLUSTER_VARIABLE) != DISPOSABLE_CLUSTER_VALUE:
        pytest.fail(
            f"refusing to run database tests: {DISPOSABLE_CLUSTER_VARIABLE} is not "
            f"{DISPOSABLE_CLUSTER_VALUE!r}. Run them with `make test`, which starts and marks the "
            "disposable test-db service; the suite creates databases and cluster-wide roles",
            pytrace=False,
        )
    marker = await connection.scalar(
        text("SELECT current_setting(:name, true)"), {"name": DISPOSABLE_SERVER_SETTING}
    )
    if marker != DISPOSABLE_SERVER_VALUE:
        pytest.fail(
            "refusing to run database tests: the server DATABASE_URL reaches is not marked as a "
            f"disposable test cluster ({DISPOSABLE_SERVER_SETTING} is {marker!r}, not "
            f"{DISPOSABLE_SERVER_VALUE!r}). Point DATABASE_URL at the test-db service, or use "
            "`make test`, which marks it; never mark a server that holds data you keep",
            pytrace=False,
        )
    if await connection.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")):
        pytest.fail(
            "refusing to run database tests: DATABASE_URL names a migrated database, which a "
            "disposable test cluster never has (is it the development database?)",
            pytrace=False,
        )


async def ensure_runtime_group(connection: AsyncConnection) -> None:
    """Create the runtime group if it is missing. Call assert_disposable_cluster() first.

    An existing group is used as is, never altered or dropped; one that could log in or is a
    superuser is refused, because 0013's grants would then reach a login.
    """
    role = (
        await connection.execute(
            text("SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = :role"),
            {"role": RUNTIME_GROUP},
        )
    ).one_or_none()
    if role is None:
        await connection.execute(
            text(
                f'CREATE ROLE "{RUNTIME_GROUP}" NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
        )
    elif role.rolcanlogin or role.rolsuper:
        pytest.fail(
            f"refusing to run database tests: role {RUNTIME_GROUP} exists but can log in or is "
            "a superuser",
            pytrace=False,
        )


async def run_alembic(engine: AsyncEngine, operation: Callable[[Config], None]) -> None:
    """Run an Alembic command on a connection of `engine` (migrations/env.py shares it)."""

    def run(connection: Connection) -> None:
        config = Config(str(ALEMBIC_INI))
        config.attributes["connection"] = connection
        operation(config)

    async with engine.begin() as connection:
        await connection.run_sync(run)


@asynccontextmanager
async def raises_violation(constraint: str) -> AsyncIterator[None]:
    """Assert that the block fails on this exact constraint, not merely on some IntegrityError."""
    with pytest.raises(IntegrityError) as excinfo:
        yield
    assert violated_constraint(excinfo.value) == constraint


async def check_deferred_constraints(session: AsyncSession) -> None:
    """Run COMMIT-time checks now. Test transactions are rolled back, so they never commit."""
    await session.flush()
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


async def audit_events_about(session: AsyncSession, account_id: uuid.UUID) -> list[AuditEvent]:
    """Posting audit events whose details name `account_id` as their (source) account, oldest
    first.

    Committed tests share the test database, so they always look at their own accounts' events,
    never at global counts.
    """
    statement = (
        select(AuditEvent)
        .where(
            AuditEvent.action.in_(POSTING_ACTIONS),
            AuditEvent.details["account_id"].astext == str(account_id),
        )
        .order_by(AuditEvent.id)
    )
    return list((await session.scalars(statement)).all())


SEQUENCE_HELPER = "public.ledger_entries_last_issued_sequence_number()"


@dataclass(frozen=True)
class TablePrivileges:
    """What the runtime group holds on one table: table-level privileges, and the columns it
    may INSERT into and UPDATE. Anything not listed is denied."""

    table: frozenset[str] = frozenset()
    insert: frozenset[str] = frozenset()
    update: frozenset[str] = frozenset()


# Migration 0013's grants to the runtime group, restated here rather than imported: the tests
# compare the catalog with this independent statement, so the migration cannot vouch for itself.
# Every table in public must appear (alembic_version with nothing): a new table needs a decision.
RUNTIME_PRIVILEGES: dict[str, TablePrivileges] = {
    "users": TablePrivileges(frozenset({"SELECT"}), frozenset({"id", "email"})),
    "user_credentials": TablePrivileges(
        frozenset({"SELECT"}),
        frozenset({"user_id", "password_hash"}),
        frozenset({"password_hash", "updated_at"}),
    ),
    "auth_sessions": TablePrivileges(
        frozenset({"SELECT"}),
        frozenset({"id", "user_id", "token_hash", "expires_at"}),
        frozenset({"revoked_at"}),
    ),
    "ledgers": TablePrivileges(frozenset({"SELECT"})),
    "accounts": TablePrivileges(
        frozenset({"SELECT"}),
        frozenset({"id", "ledger_id", "user_id", "kind"}),
        frozenset({"balance_minor"}),
    ),
    "transactions": TablePrivileges(
        frozenset({"SELECT"}),
        frozenset(
            {"id", "ledger_id", "kind", "entry_count", "idempotency_user_id", "idempotency_key"}
        ),
    ),
    "ledger_entries": TablePrivileges(
        frozenset({"SELECT"}),
        frozenset({"id", "transaction_id", "account_id", "ledger_id", "amount_minor"}),
    ),
    "idempotency_keys": TablePrivileges(
        frozenset({"SELECT"}),
        frozenset({"user_id", "key", "request_fingerprint", "transaction_id"}),
    ),
    "audit_events": TablePrivileges(
        insert=frozenset(
            {
                "actor_user_id",
                "action",
                "outcome",
                "reason",
                "transaction_id",
                "idempotency_key",
                "details",
            }
        )
    ),
    "alembic_version": TablePrivileges(),
}

# The transactions columns migration 0011 added: absent from older schemas.
IDEMPOTENCY_LINK_COLUMNS = frozenset({"idempotency_user_id", "idempotency_key"})


def _column_grants(role: str, table: str, privileges: TablePrivileges) -> list[str]:
    grants = []
    if privileges.table:
        grants.append(f'GRANT {", ".join(sorted(privileges.table))} ON {table} TO "{role}"')
    if privileges.insert:
        grants.append(
            f'GRANT INSERT ({", ".join(sorted(privileges.insert))}) ON {table} TO "{role}"'
        )
    if privileges.update:
        grants.append(
            f'GRANT UPDATE ({", ".join(sorted(privileges.update))}) ON {table} TO "{role}"'
        )
    return grants


def posting_role_grants(
    role: str, *, helper: bool = True, idempotency: bool = True, schema_before_0011: bool = False
) -> list[str]:
    """The production runtime privileges (RUNTIME_PRIVILEGES, migration 0013) on the tables the
    posting services use, plus EXECUTE on the sequence helper (migration 0007): exactly what the
    application's role holds there, so the posting tests run under it. Deliberately no privilege
    on any sequence: inserting into an identity column does not need one.

    Not granted: INSERT on accounts (opening accounts is not posting), anything on the
    authentication tables. In particular SELECT on audit_events is NOT granted: the application
    writes audit events but never reads them.

    `idempotency=False` leaves out idempotency_keys (migration 0010), for schemas older than 0010
    and for the test showing that a keyed posting then fails loudly. `schema_before_0011=True`
    leaves out the transactions columns migration 0011 added, which older schemas do not have.
    """
    posting = {
        # users: the audit insert resolves its actor (ADR 0011).
        "users": TablePrivileges(RUNTIME_PRIVILEGES["users"].table),
        "ledgers": RUNTIME_PRIVILEGES["ledgers"],
        # UPDATE on balance_minor only: the balance updates, and the FOR UPDATE row locks
        # (protocol and trigger), which need UPDATE on at least one column. Never on user_id,
        # kind or ledger_id: ownership and account identity are not the posting role's to
        # change, and such a change would leave no ledger entry or audit event.
        "accounts": TablePrivileges(
            RUNTIME_PRIVILEGES["accounts"].table, update=RUNTIME_PRIVILEGES["accounts"].update
        ),
        "transactions": TablePrivileges(
            RUNTIME_PRIVILEGES["transactions"].table,
            RUNTIME_PRIVILEGES["transactions"].insert
            - (IDEMPOTENCY_LINK_COLUMNS if schema_before_0011 else frozenset()),
        ),
        "ledger_entries": RUNTIME_PRIVILEGES["ledger_entries"],
        "audit_events": RUNTIME_PRIVILEGES["audit_events"],
    }
    if idempotency:
        # INSERT: the claim. SELECT: the claim's ON CONFLICT check and the replay lookup. Never
        # UPDATE or DELETE: a consumed key is permanent (ADR 0006).
        posting["idempotency_keys"] = RUNTIME_PRIVILEGES["idempotency_keys"]
    grants = [
        grant
        for table, privileges in posting.items()
        for grant in _column_grants(role, table, privileges)
    ]
    if helper:
        grants.append(f'GRANT EXECUTE ON FUNCTION {SEQUENCE_HELPER} TO "{role}"')
    return grants


async def bypass_triggers(session: AsyncSession) -> None:
    """Disable every trigger (immutability, deferred checks, foreign keys) for the rest of the
    current transaction. TEST-ONLY, and only inside a transaction that is rolled back.

    session_replication_role = replica is how a superuser skips triggers, so tests use it both
    to plant corruption that the schema would otherwise refuse (reconciliation must still find
    it) and to demonstrate that trigger protection does not bind a superuser (ADR 0004).
    """
    is_superuser = await session.scalar(
        text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
    )
    if not is_superuser:
        pytest.fail("this test needs a superuser test role to bypass triggers", pytrace=False)
    await session.execute(text("SET LOCAL session_replication_role = replica"))


async def assert_reconciled(session: AsyncSession) -> None:
    """Every balance equals its entries, every ledger sums to zero (entries and balances), every
    transaction is valid and every entry sits in its account's and transaction's ledger.

    Global by design: tests that COMMIT must leave the shared test database reconciled. Audit
    coverage is checked per test instead (some schema tests commit transactions directly).
    """
    assert await find_balance_mismatches(session) == []
    assert await find_unbalanced_ledgers(session) == {}
    assert await find_ledgers_with_nonzero_balances(session) == {}
    assert await find_invalid_transactions(session) == []
    assert await find_misplaced_entries(session) == []
