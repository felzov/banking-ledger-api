"""HTTP answers when no database connection can be obtained (Phase 9 A3).

An unreachable database or an exhausted pool is 503 temporarily_unavailable, the same body as
any other temporary unavailability, on every route: login, an authenticated route, a posting.
A refusal PostgreSQL itself reports, a connection lost mid-transaction and any other error keep
their existing answer, 500 internal_error.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import asyncpg  # type: ignore[import-untyped]  # asyncpg ships no type information
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from ledger_api.domain.errors import TemporarilyUnavailableError
from ledger_api.domain.tokens import generate_token
from ledger_api.main import create_app
from tests.conftest import UNREACHABLE_DATABASE_URL, settings_for
from tests.database import configured_database_url, disposable_database_url
from tests.unit.test_commit_outcome_classification import _as_sqlalchemy_error

UNAVAILABLE = {
    "code": "temporarily_unavailable",
    "detail": TemporarilyUnavailableError.message,
}
INTERNAL_ERROR = {"code": "internal_error", "detail": "Internal server error."}


@asynccontextmanager
async def _observing(app: FastAPI) -> AsyncIterator[AsyncClient]:
    # raise_app_exceptions=False: observe a 500; Starlette re-raises it for the server log.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        yield client


def _bearer() -> dict[str, str]:
    # Well-formed, so the request reaches the token lookup (and the database).
    return {"Authorization": f"Bearer {generate_token().token}"}


async def test_login_without_a_database_is_503(client_without_database: AsyncClient) -> None:
    response = await client_without_database.post(
        "/sessions", json={"email": "someone@example.com", "password": "x" * 12}
    )

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)


async def test_an_authenticated_route_without_a_database_is_503(
    client_without_database: AsyncClient,
) -> None:
    response = await client_without_database.get(
        f"/users/{uuid.uuid7()}/accounts", headers=_bearer()
    )

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)


async def test_a_posting_without_a_database_is_503(client_without_database: AsyncClient) -> None:
    response = await client_without_database.post(
        f"/users/{uuid.uuid7()}/accounts/{uuid.uuid7()}/withdrawals",
        headers={**_bearer(), "Idempotency-Key": f"a3-{uuid.uuid7().hex}"},
        json={"amount_minor": 100},
    )

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)


async def test_a_database_that_refuses_the_connection_itself_is_still_500() -> None:
    # Reached, but the server refused (unknown database): a misconfiguration, not an outage.
    url = disposable_database_url(configured_database_url(), "_never_created")
    async with _observing(create_app(settings_for(url))) as client:
        response = await client.post(
            "/sessions", json={"email": "someone@example.com", "password": "x" * 12}
        )

    assert (response.status_code, response.json()) == (500, INTERNAL_ERROR)


def _raising(error: Exception) -> Callable[[], None]:
    def route() -> None:
        raise error

    return route


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PoolTimeoutError("QueuePool limit of size 5 overflow 10 reached"), (503, UNAVAILABLE)),
        # A connection lost mid-transaction: outside postings, nothing tells whether COMMIT
        # was sent, so it is not reported as "nothing happened".
        (
            _as_sqlalchemy_error(asyncpg.InterfaceError("connection is closed"), invalidated=True),
            (500, INTERNAL_ERROR),
        ),
        # A raw OSError is not the database's to classify.
        (ConnectionRefusedError(111, "refused"), (500, INTERNAL_ERROR)),
        (FileNotFoundError("/nonexistent"), (500, INTERNAL_ERROR)),
    ],
    ids=["pool-timeout", "connection-lost", "raw-oserror", "file-not-found"],
)
async def test_only_acquisition_failures_answer_503(
    error: Exception, expected: tuple[int, dict[str, str]]
) -> None:
    app = create_app(settings_for(UNREACHABLE_DATABASE_URL))
    app.add_api_route("/test-only/fail", _raising(error))
    async with _observing(app) as client:
        response = await client.get("/test-only/fail")

    assert (response.status_code, response.json()) == expected
