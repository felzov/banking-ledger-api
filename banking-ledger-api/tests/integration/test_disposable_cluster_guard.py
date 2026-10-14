"""The test suite's own guard: databases and roles only on a disposable cluster (Phase 10).

assert_disposable_cluster() (tests.database) requires, independently: the environment's intent
(LEDGER_TEST_CLUSTER), the marker in the configuration of the server DATABASE_URL actually reaches
(ledger.test_cluster, set by `make test` / CI on the throwaway server, never through
DATABASE_URL), and an unmigrated database. Each test breaks exactly one of them, as a
misconfiguration would, and checks that the suite fails closed, before creating anything.

The suite itself runs on the marked test-db server, so a server WITHOUT the marker is simulated
by asking for another setting name (no server has it) or another value: a development server is
never touched.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from tests import database
from tests.database import (
    DISPOSABLE_CLUSTER_VARIABLE,
    DISPOSABLE_SERVER_SETTING,
    assert_disposable_cluster,
    configured_database_url,
    recreate_database,
)

# Never created: every case below must fail before the CREATE DATABASE.
GUARD_DATABASE = "ledger_guard_never_created_test"


async def _database_exists(server: URL, name: str) -> bool:
    engine = create_async_engine(server, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return bool(
                await connection.scalar(
                    text("SELECT count(*) FROM pg_database WHERE datname = :name"),
                    {"name": name},
                )
            )
    finally:
        await engine.dispose()


async def _refused(match: str) -> None:
    server = configured_database_url()
    with pytest.raises(pytest.fail.Exception, match=match):
        await recreate_database(server, GUARD_DATABASE)
    assert not await _database_exists(server, GUARD_DATABASE)


@pytest.fixture
async def server_connection() -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(configured_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def test_the_real_test_server_is_recognised(server_connection: AsyncConnection) -> None:
    # The positive case every other database test relies on: all three checks pass here.
    await assert_disposable_cluster(server_connection)
    marker = await server_connection.scalar(
        text("SELECT current_setting(:name, true)"), {"name": DISPOSABLE_SERVER_SETTING}
    )
    assert marker == "disposable"


async def test_missing_environment_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DISPOSABLE_CLUSTER_VARIABLE)

    await _refused(f"{DISPOSABLE_CLUSTER_VARIABLE} is not 'disposable'")


@pytest.mark.parametrize("value", ["", "yes", "true", "1", "Disposable", "disposable "])
async def test_misleading_environment_marker(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(DISPOSABLE_CLUSTER_VARIABLE, value)

    await _refused(f"{DISPOSABLE_CLUSTER_VARIABLE} is not 'disposable'")


async def test_environment_marker_alone_proves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # The edge case this guard closes: LEDGER_TEST_CLUSTER=disposable, but DATABASE_URL reaches
    # a server that is not marked (here: one without the setting at all, as any development
    # server is). The environment variable alone does not get through.
    monkeypatch.setenv(DISPOSABLE_CLUSTER_VARIABLE, "disposable")
    monkeypatch.setattr(database, "DISPOSABLE_SERVER_SETTING", "ledger.not_a_test_cluster")

    await _refused("not marked as a disposable test cluster .*None")


async def test_inconsistent_server_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # A server marked with something else than the expected value is not a test server either.
    monkeypatch.setattr(database, "DISPOSABLE_SERVER_VALUE", "disposable-elsewhere")

    await _refused("not marked as a disposable test cluster .*'disposable'")


async def test_migrated_database(cluster_database: URL) -> None:
    # Environment and server markers both pass, but the database holds an application schema,
    # as a development database would.
    engine = create_async_engine(cluster_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            with pytest.raises(pytest.fail.Exception, match="migrated database"):
                await assert_disposable_cluster(connection)
    finally:
        await engine.dispose()


@pytest.fixture
def cluster_database(engine: object, test_database_url: URL) -> URL:
    # The shared test database: migrated by the `engine` fixture.
    return test_database_url
