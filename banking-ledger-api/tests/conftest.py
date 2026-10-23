import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from alembic import command
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from ledger_api.api.dependencies import get_session
from ledger_api.config import Settings
from ledger_api.data.engine import create_engine
from ledger_api.data.rate_limits import connect as connect_redis
from ledger_api.main import create_app
from tests.auth_helpers import OWNER_TOKENS, authenticate_as_path_owner
from tests.database import (
    configured_database_url,
    disposable_database_url,
    recreate_database,
    run_alembic,
)

# Valid asyncpg URL on a port where nothing listens: the app starts, the database is "down".
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://ledger:synthetic@127.0.0.1:1/ledger"


def settings_for(database_url: URL | str, **overrides: Any) -> Settings:
    if isinstance(database_url, URL):
        database_url = database_url.render_as_string(hide_password=False)
    # Cheapest Argon2id parameters: the suite tests the protocol, not Argon2's cost. Only
    # allowed outside production (Settings refuses them there). No rate limits unless a test
    # asks for them (redis_url): settings would otherwise read REDIS_URL from the environment.
    fields: dict[str, Any] = {
        "app_env": "test",
        "database_url": SecretStr(database_url),
        "argon2_time_cost": 1,
        "argon2_memory_cost_kib": 8,
        "argon2_parallelism": 1,
        "redis_url": None,
    }
    return Settings(**(fields | overrides))


@pytest.fixture
def test_redis_url() -> str:
    """The rate-limit tests' Redis (`make test` starts it). Fails rather than skips without it,
    like the database: a missing service must not turn into silently untested limits."""
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.fail("TEST_REDIS_URL is not set: run the suite with `make test`")
    return url


@pytest.fixture
async def rate_limit_namespace(test_redis_url: str) -> AsyncIterator[str]:
    """A key namespace of this test's own, deleted afterwards: tests never see each other's
    counters, and nothing else in that Redis is ever touched."""
    namespace = f"test-{uuid.uuid7().hex}"
    yield namespace
    client = connect_redis(test_redis_url)
    try:
        keys = [key async for key in client.scan_iter(match=f"{namespace}:*")]
        if keys:
            await client.delete(*keys)
    finally:
        await client.aclose()


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
    # The suite's own superuser connection: the startup privilege guard (ADR 0015) would refuse
    # it, by design; only this Python-level hook turns it off.
    engine = create_engine(settings_for(test_database_url), privilege_guard=False)
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
    return create_app(settings_for(test_database_url), privilege_guard=False)  # superuser


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with serve(app) as client:
        yield client


@pytest.fixture
async def client_without_database() -> AsyncIterator[AsyncClient]:
    async with serve(create_app(settings_for(UNREACHABLE_DATABASE_URL))) as client:
        yield client


@pytest.fixture
async def api_client(app: FastAPI, new_session: SessionFactory) -> AsyncIterator[AsyncClient]:
    """Client whose requests run on the test's rolled-back connection.

    Each request gets a fresh session there, so services still open and commit their own
    transactions, but a COMMIT only releases a SAVEPOINT and the test leaves no data. The
    real get_session (which commits for real) is covered by test_session_dependency.py.
    """

    async def session_on_test_connection() -> AsyncIterator[AsyncSession]:
        async with new_session() as session:
            yield session

    app.dependency_overrides[get_session] = session_on_test_connection
    async with serve(app) as client:
        # Acts as the owner of a /users/{user_id} path only for users the test signed up or
        # granted a token (tests.auth_helpers); everything else goes out unauthenticated.
        tokens: dict[str, str] = {}
        OWNER_TOKENS[client] = tokens
        client.event_hooks["request"].append(authenticate_as_path_owner(tokens))
        yield client


@pytest.fixture
async def anonymous_client(api_client: AsyncClient, app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A second client on the same app and rolled-back connection, never adding a token."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client
