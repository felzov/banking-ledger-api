from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
from alembic import command
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from ledger_api.config import Settings
from ledger_api.data.engine import create_engine
from ledger_api.main import create_app
from tests.database import (
    configured_database_url,
    disposable_database_url,
    recreate_database,
    run_alembic,
)

# Valid asyncpg URL on a port where nothing listens: the app starts, the database is "down".
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://ledger:synthetic@127.0.0.1:1/ledger"


def settings_for(database_url: URL | str) -> Settings:
    if isinstance(database_url, URL):
        database_url = database_url.render_as_string(hide_password=False)
    return Settings(app_env="test", database_url=SecretStr(database_url))


@pytest.fixture(scope="session")
def test_database_url() -> URL:
    return disposable_database_url(configured_database_url(), "")


@pytest.fixture(scope="session")
async def engine(test_database_url: URL) -> AsyncIterator[AsyncEngine]:
    """A freshly created and migrated test database, built once per test run.

    The schema comes from the Alembic migrations, never from metadata.create_all(): the
    ledger invariants live in triggers that only the migrations install.
    """
    assert test_database_url.database is not None
    await recreate_database(configured_database_url(), test_database_url.database)
    engine = create_engine(settings_for(test_database_url))
    await run_alembic(engine, lambda config: command.upgrade(config, "head"))
    yield engine
    await engine.dispose()


type SessionFactory = Callable[[], AsyncSession]


@pytest.fixture
async def db_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """One connection inside an outer transaction that is always rolled back.

    Every session a test creates is bound to it, so tests leave no data behind.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()


@pytest.fixture
def new_session(db_connection: AsyncConnection) -> SessionFactory:
    """Factory for fresh sessions on the test connection, e.g. one per service call.

    Services open their own transaction with session.begin(), which requires a session that
    has not begun one yet. With create_savepoint, their COMMIT only releases a SAVEPOINT.
    """

    def factory() -> AsyncSession:
        return AsyncSession(
            bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )

    return factory


@pytest.fixture
async def db_session(new_session: SessionFactory) -> AsyncIterator[AsyncSession]:
    """A session for test setup and assertions; everything is rolled back afterwards."""
    session = new_session()
    try:
        yield session
    finally:
        await session.close()


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client for `app` with its lifespan running.

    httpx's ASGITransport does not send lifespan events, so without this the engine would
    never be created.
    """
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        yield client


@pytest.fixture
def app(engine: AsyncEngine, test_database_url: URL) -> FastAPI:
    # Depends on `engine` so the test database exists and is migrated; the app builds its own.
    return create_app(settings_for(test_database_url))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with serve(app) as client:
        yield client


@pytest.fixture
async def client_without_database() -> AsyncIterator[AsyncClient]:
    async with serve(create_app(settings_for(UNREACHABLE_DATABASE_URL))) as client:
        yield client
