"""PostgreSQL helpers for the test suite: disposable databases, migrations, violation checks."""

import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import URL, Connection, make_url, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
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
    assert_disposable(database)
    # CREATE/DROP DATABASE cannot run inside a transaction block, hence AUTOCOMMIT.
    engine = create_async_engine(server_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            quoted = connection.dialect.identifier_preparer.quote(database)
            await connection.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
            await connection.execute(text(f"CREATE DATABASE {quoted}"))
    finally:
        await engine.dispose()


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


def posting_role_grants(role: str, *, helper: bool = True) -> list[str]:
    """The privileges a least-privilege role needs to run the posting services (ADR 0012):
    table privileges, plus EXECUTE on the sequence helper (migration 0007). Deliberately no
    privilege on any sequence: inserting into an identity column does not need one.
    """
    grants = [
        f'GRANT SELECT ON users, ledgers TO "{role}"',
        # UPDATE on balance_minor only: the balance updates, and the FOR UPDATE row locks
        # (protocol and trigger), which need UPDATE on at least one column. Never on user_id,
        # kind or ledger_id: ownership and account identity are not the posting role's to
        # change, and such a change would leave no ledger entry or audit event.
        f'GRANT SELECT ON accounts TO "{role}"',
        f'GRANT UPDATE (balance_minor) ON accounts TO "{role}"',
        # SELECT: RETURNING clauses and the deferred COMMIT-time checks.
        f'GRANT SELECT, INSERT ON transactions, ledger_entries, audit_events TO "{role}"',
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
