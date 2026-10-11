"""An authenticated request keeps the service-owned transaction boundaries (ADR 0001, ADR 0013).

Every protected request runs several transactions on one request session: the authentication
lookup, then the route's service (a posting: key claim and ledger writes, then possibly a
rejection audit). Through the real HTTP stack, with real COMMITs and a pool of exactly ONE
connection: if authentication left its transaction open, the service's session.begin() would
fail; if anything held a second connection, the request would time out. After every request
the session must be outside any transaction and the connection back in the pool.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import QueuePool

from ledger_api.api.dependencies import get_session
from ledger_api.data.engine import create_sessionmaker
from tests.auth_helpers import TEST_PASSWORD
from tests.conftest import serve


@pytest.fixture
async def single_connection_engine(
    engine: AsyncEngine, test_database_url: URL
) -> AsyncIterator[AsyncEngine]:
    single = create_async_engine(test_database_url, pool_size=1, max_overflow=0, pool_timeout=1)
    try:
        yield single
    finally:
        await single.dispose()


@pytest.fixture
async def observed(
    app: FastAPI, single_connection_engine: AsyncEngine
) -> AsyncIterator[tuple[AsyncClient, list[bool]]]:
    """A client on the real app whose request sessions come from the one-connection engine,
    and, per request, whether its session was left inside a transaction."""
    sessionmaker = create_sessionmaker(single_connection_engine)
    left_in_transaction: list[bool] = []

    async def session_from_single_connection() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            try:
                yield session
            finally:
                # In finally: on an error response, the exception is raised at the yield.
                left_in_transaction.append(session.in_transaction())

    app.dependency_overrides[get_session] = session_from_single_connection
    try:
        async with serve(app) as client:
            yield client, left_in_transaction
    finally:
        app.dependency_overrides.clear()


async def test_every_authenticated_request_leaves_no_transaction_or_connection_behind(
    observed: tuple[AsyncClient, list[bool]], single_connection_engine: AsyncEngine
) -> None:
    client, left_in_transaction = observed
    pool = single_connection_engine.pool
    assert isinstance(pool, QueuePool)
    email = f"sessions-{uuid.uuid7().hex}@example.com"

    async def request(method: str, url: str, expected: int, **options: Any) -> Any:
        response = await client.request(method, url, **options)
        assert response.status_code == expected, (url, response.text)
        assert left_in_transaction[-1] is False, url
        assert pool.checkedout() == 0, url
        return response.json() if response.content else None

    user = await request("POST", "/users", 201, json={"email": email, "password": TEST_PASSWORD})
    login = await request(
        "POST", "/sessions", 201, json={"email": email, "password": TEST_PASSWORD}
    )
    auth = {"Authorization": f"Bearer {login['access_token']}"}
    base = f"/users/{user['id']}/accounts"
    account = await request("POST", base, 201, json={"currency": "GBP"}, headers=auth)
    account_url = f"{base}/{account['id']}"
    deposit = await request(
        "POST",
        f"{account_url}/deposits",
        201,
        json={"amount_minor": 700},
        headers={**auth, "Idempotency-Key": f"k-{uuid.uuid7().hex}"},
    )
    # A rejected posting: authentication, the failed posting transaction, then the rejection
    # audit in a transaction of its own, all on the one connection.
    await request(
        "POST",
        f"{account_url}/withdrawals",
        422,
        json={"amount_minor": 10_000},
        headers={**auth, "Idempotency-Key": f"k-{uuid.uuid7().hex}"},
    )
    await request("GET", f"{account_url}/statement", 200, headers=auth)
    await request(
        "GET", f"{account_url}/transactions/{deposit['transaction_id']}", 200, headers=auth
    )
    await request("GET", account_url, 200, headers=auth)
    await request("GET", f"/users/{user['id']}", 200, headers=auth)
    # Refused requests release everything too.
    await request("GET", f"/users/{uuid.uuid7()}", 403, headers=auth)
    await request("GET", account_url, 401, headers={"Authorization": "Bearer lt_bogus"})
    await request("DELETE", "/sessions/current", 204, headers=auth)
    await request("GET", account_url, 401, headers=auth)

    assert len(left_in_transaction) == 13
