"""PostgreSQL helpers for the test suite: disposable databases, migrations, violation checks."""

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import URL, Connection, make_url, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_api.data.errors import violated_constraint

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
