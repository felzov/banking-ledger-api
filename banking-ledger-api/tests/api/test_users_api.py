import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


async def _create_user(client: AsyncClient, email: str) -> dict[str, Any]:
    response = await client.post("/users", json={"email": email})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _assert_validation_error_without_echo(body: dict[str, Any], *submitted: str) -> None:
    assert body["code"] == "validation_error"
    assert body["detail"]
    for error in body["detail"]:
        assert set(error) <= {"type", "loc", "msg"}  # no "input", no "ctx"
    for value in submitted:
        assert value not in str(body)


# --- POST /users -------------------------------------------------------------------------------


async def test_create_user_returns_201_with_location_and_the_user(api_client: AsyncClient) -> None:
    response = await api_client.post("/users", json={"email": "alice@example.com"})

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "created_at"}
    assert body["email"] == "alice@example.com"
    assert uuid.UUID(body["id"]).version == 7
    assert response.headers["location"] == f"/users/{body['id']}"


async def test_create_user_stores_the_canonical_email(api_client: AsyncClient) -> None:
    body = await _create_user(api_client, "  Alice.Smith@Example.COM ")

    assert body["email"] == "alice.smith@example.com"
    fetched = await api_client.get(f"/users/{body['id']}")
    assert fetched.json()["email"] == "alice.smith@example.com"


async def test_duplicate_email_in_any_case_returns_409(api_client: AsyncClient) -> None:
    await _create_user(api_client, "bob@example.com")

    response = await api_client.post("/users", json={"email": "BOB@Example.com"})

    assert response.status_code == 409
    assert response.json() == {
        "code": "email_already_registered",
        "detail": "A user with this email address already exists.",
    }


@pytest.mark.parametrize(
    "email",
    ["not-an-email", "secret-name@", "secret-name@localhost", "sécret@example.com"],
)
async def test_invalid_email_returns_422_without_echoing_it(
    api_client: AsyncClient, email: str
) -> None:
    response = await api_client.post("/users", json={"email": email})

    assert response.status_code == 422
    _assert_validation_error_without_echo(response.json(), email, "secret")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": None},
        {"email": 42},
        {"email": "x" * 321},
        {"email": "carol@example.com", "id": str(uuid.uuid7())},  # extra fields are rejected
    ],
)
async def test_malformed_create_user_requests_return_422(
    api_client: AsyncClient, payload: dict[str, Any]
) -> None:
    response = await api_client.post("/users", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_non_json_body_returns_422(api_client: AsyncClient) -> None:
    response = await api_client.post(
        "/users", content=b"email=a@example.com", headers={"content-type": "text/plain"}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --- GET /users/{user_id} ----------------------------------------------------------------------


async def test_get_user_returns_the_user(api_client: AsyncClient) -> None:
    created = await _create_user(api_client, "dave@example.com")

    response = await api_client.get(f"/users/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


async def test_unknown_user_returns_404(api_client: AsyncClient) -> None:
    response = await api_client.get(f"/users/{uuid.uuid7()}")

    assert response.status_code == 404
    assert response.json() == {"code": "user_not_found", "detail": "User not found."}


async def test_malformed_user_id_returns_422(api_client: AsyncClient) -> None:
    response = await api_client.get("/users/not-a-uuid")

    assert response.status_code == 422
    _assert_validation_error_without_echo(response.json(), "not-a-uuid")


# --- unexpected errors -------------------------------------------------------------------------


async def test_unexpected_error_returns_a_generic_500(app: FastAPI) -> None:
    async def explode() -> None:
        raise RuntimeError("internal detail: connection string, stack, ...")

    app.add_api_route("/test-only/explode", explode)
    # raise_app_exceptions=False: observe the response; Starlette re-raises for the server log.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/test-only/explode")

    assert response.status_code == 500
    assert response.json() == {"code": "internal_error", "detail": "Internal server error."}
