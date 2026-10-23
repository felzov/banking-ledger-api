"""Request correlation over HTTP (ADR 0017): the X-Request-ID header, the audit trail's
request_id, and the request id and absence of secrets in every log line."""

import io
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import AuditEvent
from ledger_api.observability.logs import configure_logging
from tests.auth_helpers import TEST_PASSWORD, signup


def _request_id(response: Any) -> str:
    value: str = response.headers["x-request-id"]
    assert uuid.UUID(value).version == 7
    return value


@pytest.fixture
def log_lines() -> Iterator[io.StringIO]:
    """The JSON log output, captured. Configured after the app's lifespan has run (the client
    fixtures start it), which would otherwise send it to stderr."""
    buffer = io.StringIO()
    configure_logging("INFO", stream=buffer)
    try:
        yield buffer
    finally:
        root = logging.getLogger()
        for handler in [h for h in root.handlers if h.get_name() == "ledger_api.json"]:
            root.removeHandler(handler)


def _entries(buffer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


async def test_every_response_carries_its_own_request_id(api_client: AsyncClient) -> None:
    responses = [
        await api_client.get("/health/live"),
        await api_client.get(f"/users/{uuid.uuid7()}/accounts"),  # no token
        await api_client.post("/users", json={}),  # malformed
        await api_client.get("/nowhere"),
    ]

    assert [r.status_code for r in responses] == [200, 401, 422, 404]
    assert len({_request_id(r) for r in responses}) == 4


async def test_a_client_cannot_choose_its_request_id(api_client: AsyncClient) -> None:
    # A client-chosen id could make unrelated requests look related in logs and audit events.
    response = await api_client.get("/health/live", headers={"X-Request-ID": "chosen-by-client"})

    assert _request_id(response) != "chosen-by-client"


async def test_an_unexpected_error_still_carries_its_request_id(app: FastAPI) -> None:
    # Starlette answers it outside the request middleware (api.errors adds the header).
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        response = await client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {"code": "internal_error", "detail": "Internal server error."}
    _request_id(response)


async def test_audit_events_record_the_request_that_caused_them(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = f"corr-{uuid.uuid7().hex}@example.com"
    credentials = {"email": email, "password": TEST_PASSWORD}
    registered = await api_client.post("/users", json=credentials)
    duplicate = await api_client.post("/users", json=credentials)
    wrong = await api_client.post("/sessions", json={**credentials, "password": "x" * 12})
    assert [r.status_code for r in (registered, duplicate, wrong)] == [201, 409, 401]

    ids = {_request_id(r): r for r in (registered, duplicate, wrong)}
    events = (
        await db_session.execute(
            select(AuditEvent.request_id, AuditEvent.action, AuditEvent.outcome).where(
                AuditEvent.request_id.in_(ids)
            )
        )
    ).all()

    assert {(e.request_id, e.action, e.outcome) for e in events} == {
        (_request_id(registered), "user.register", "succeeded"),
        (_request_id(duplicate), "user.register", "rejected"),
        (_request_id(wrong), "auth.login", "rejected"),
    }


async def test_every_log_line_of_a_request_carries_its_id_and_no_secret(
    api_client: AsyncClient, log_lines: io.StringIO
) -> None:
    caller = await signup(api_client)
    logout = await api_client.delete("/sessions/current", headers=caller.headers)
    assert logout.status_code == 204

    access = [e for e in _entries(log_lines) if e["logger"] == "ledger_api.access"]
    assert [(e["method"], e["path"], e["status"]) for e in access] == [
        ("POST", "/users", 201),
        ("POST", "/sessions", 201),
        ("DELETE", "/sessions/current", 204),
    ]
    assert access[-1]["request_id"] == _request_id(logout)
    assert all(e["duration_ms"] >= 0 for e in access)
    # Nothing that identifies or authenticates the user (ADR 0013).
    text = log_lines.getvalue()
    for secret in (caller.email, TEST_PASSWORD, caller.token):
        assert secret not in text
