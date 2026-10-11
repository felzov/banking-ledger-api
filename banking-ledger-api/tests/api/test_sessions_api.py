"""POST /users (with a password), POST /sessions and DELETE /sessions/current (ADR 0013).

Requests run on the test's rolled-back connection (api_client): nothing outlives the test.
The app's Argon2id parameters are the cheapest (tests.conftest.settings_for).
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import AuthSession
from ledger_api.domain.tokens import generate_token, hash_token, is_well_formed_token
from tests.auth_helpers import TEST_PASSWORD

INVALID_CREDENTIALS = {"code": "invalid_credentials", "detail": "Invalid email or password."}
INVALID_TOKEN = {
    "code": "invalid_token",
    "detail": "The bearer token is invalid, expired or revoked.",
}
AUTHENTICATION_REQUIRED = {
    "code": "authentication_required",
    "detail": "This request requires a bearer token.",
}


def _email() -> str:
    return f"s-{uuid.uuid7().hex}@example.com"


async def _register(client: AsyncClient, email: str, password: str = TEST_PASSWORD) -> Response:
    return await client.post("/users", json={"email": email, "password": password})


async def _login(client: AsyncClient, email: str, password: str = TEST_PASSWORD) -> Response:
    return await client.post("/sessions", json={"email": email, "password": password})


async def _logout(client: AsyncClient, headers: dict[str, str] | None = None) -> Response:
    return await client.delete("/sessions/current", headers=headers or {})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- registration -------------------------------------------------------------------------------


async def test_registration_returns_the_user_without_the_password(api_client: AsyncClient) -> None:
    email = _email()

    response = await _register(api_client, email)

    assert response.status_code == 201
    assert set(response.json()) == {"id", "email", "created_at"}
    assert TEST_PASSWORD not in response.text


@pytest.mark.parametrize("password", ["short", "x" * 129, "elevenchars"])
async def test_a_password_outside_the_policy_is_422_and_never_echoed(
    api_client: AsyncClient, password: str
) -> None:
    response = await _register(api_client, _email(), password)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert all(set(error) <= {"type", "loc", "msg"} for error in body["detail"])
    assert password not in response.text
    assert any(error["loc"][-1] == "password" for error in body["detail"])


async def test_a_duplicate_email_is_still_409(api_client: AsyncClient) -> None:
    email = _email()
    await _register(api_client, email)

    response = await _register(api_client, email, "another correct battery")

    assert (response.status_code, response.json()["code"]) == (409, "email_already_registered")


# --- login --------------------------------------------------------------------------------------


async def test_login_returns_a_bearer_token_that_is_never_cached(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = _email()
    await _register(api_client, email)

    response = await _login(api_client, email)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_at"}
    assert body["token_type"] == "bearer"  # noqa: S105 (the OAuth token type)
    assert is_well_formed_token(body["access_token"])
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert TEST_PASSWORD not in response.text
    # Only the digest is stored, with a 30-minute expiry in database time.
    stored = await db_session.scalar(
        select(AuthSession).where(AuthSession.token_hash == hash_token(body["access_token"]))
    )
    assert stored is not None
    assert (stored.expires_at - stored.created_at).total_seconds() == 1_800


async def test_an_unknown_email_and_a_wrong_password_answer_identically(
    api_client: AsyncClient,
) -> None:
    email = _email()
    await _register(api_client, email)

    wrong = await _login(api_client, email, "wrong horse battery staple")
    unknown = await _login(api_client, _email())

    for response in (wrong, unknown):
        assert response.status_code == 401
        assert response.json() == INVALID_CREDENTIALS
        assert response.headers["www-authenticate"] == "Bearer"
    assert wrong.content == unknown.content
    assert "wrong horse battery staple" not in wrong.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"email": "someone@example.com"},
        {"password": TEST_PASSWORD},
        {"email": "not-an-email", "password": TEST_PASSWORD},
        {"email": "someone@example.com", "password": TEST_PASSWORD, "remember_me": True},
        {"email": "someone@example.com", "password": "x" * 1_025},
    ],
)
async def test_malformed_login_requests_are_422_without_echo(
    api_client: AsyncClient, body: dict[str, Any]
) -> None:
    response = await api_client.post("/sessions", json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert TEST_PASSWORD not in response.text


# --- logout -------------------------------------------------------------------------------------


async def test_logout_revokes_the_token_and_a_repeat_is_invalid_token(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = _email()
    await _register(api_client, email)
    token = (await _login(api_client, email)).json()["access_token"]

    first = await _logout(api_client, _bearer(token))
    again = await _logout(api_client, _bearer(token))

    assert (first.status_code, first.content) == (204, b"")
    assert again.status_code == 401
    assert again.json() == INVALID_TOKEN
    assert again.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    stored = await db_session.scalar(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    )
    assert stored is not None
    assert stored.revoked_at is not None
    assert token not in again.text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Token abc"},
    ],
    ids=["missing", "empty", "scheme-only", "scheme-and-space", "basic", "other-scheme"],
)
async def test_logout_without_a_bearer_token_is_authentication_required(
    api_client: AsyncClient, headers: dict[str, str]
) -> None:
    response = await _logout(api_client, headers)

    assert response.status_code == 401
    assert response.json() == AUTHENTICATION_REQUIRED
    assert response.headers["www-authenticate"] == "Bearer"


async def test_two_authorization_headers_are_authentication_required(
    api_client: AsyncClient,
) -> None:
    token = generate_token().token

    response = await api_client.delete(
        "/sessions/current",
        headers=[("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")],
    )

    assert (response.status_code, response.json()) == (401, AUTHENTICATION_REQUIRED)


@pytest.mark.parametrize(
    "token",
    [generate_token().token, "lt_short", "not-a-token-at-all"],
    ids=["unknown", "malformed", "garbage"],
)
async def test_logout_with_an_invalid_token_is_invalid_token(
    api_client: AsyncClient, token: str
) -> None:
    response = await _logout(api_client, _bearer(token))

    assert (response.status_code, response.json()) == (401, INVALID_TOKEN)
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    assert token not in response.text


async def test_the_bearer_scheme_is_case_insensitive(api_client: AsyncClient) -> None:
    email = _email()
    await _register(api_client, email)
    token = (await _login(api_client, email)).json()["access_token"]

    response = await _logout(api_client, {"Authorization": f"bearer {token}"})

    assert response.status_code == 204


# --- OpenAPI ------------------------------------------------------------------------------------


async def test_openapi_documents_registration_login_and_logout(api_client: AsyncClient) -> None:
    schema = (await api_client.get("/openapi.json")).json()
    components = schema["components"]

    assert set(schema["paths"]["/sessions"]) == {"post"}
    assert set(schema["paths"]["/sessions/current"]) == {"delete"}
    login = schema["paths"]["/sessions"]["post"]
    assert {"201", "401", "422"} <= set(login["responses"])
    logout = schema["paths"]["/sessions/current"]["delete"]
    assert {"204", "401"} <= set(logout["responses"])
    assert logout["security"] == [{"HTTPBearer": []}]
    assert components["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "An access token from POST /sessions.",
    }
    # The password is a write-only secret in both request schemas.
    for model in ("CreateUserRequest", "LoginRequest"):
        password = components["schemas"][model]["properties"]["password"]
        assert password["format"] == "password"
        assert password["writeOnly"] is True
    assert set(components["schemas"]["LoginResponse"]["properties"]) == {
        "access_token",
        "token_type",
        "expires_at",
    }
