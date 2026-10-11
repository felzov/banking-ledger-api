"""Authentication and authorization on every route (ADR 0009, ADR 0013).

The route inventory below is explicit and must match the application exactly: a new route
fails test_the_route_inventory_is_exactly_known until it is classified here, and every protected
route is then exercised by every test that follows (no subset, no sampling).

Requests run on the test's rolled-back connection. `api_client` acts as a path's owner only for
users the test signed up; these tests pass explicit headers, or use `anonymous_client`.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import Account, AuditEvent, AuthSession, IdempotencyKey, LedgerEntry
from ledger_api.domain.tokens import generate_token, hash_token
from tests.auth_helpers import Caller, signup

PROTECTED = {
    ("GET", "/users/{user_id}"),
    ("POST", "/users/{user_id}/accounts"),
    ("GET", "/users/{user_id}/accounts"),
    ("GET", "/users/{user_id}/accounts/{account_id}"),
    ("GET", "/users/{user_id}/accounts/{account_id}/statement"),
    ("GET", "/users/{user_id}/accounts/{account_id}/transactions/{transaction_id}"),
    ("POST", "/users/{user_id}/accounts/{account_id}/deposits"),
    ("POST", "/users/{user_id}/accounts/{account_id}/withdrawals"),
    ("POST", "/users/{user_id}/accounts/{account_id}/transfers"),
}
PUBLIC = {
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
    ("POST", "/users"),
    ("POST", "/sessions"),
}
# Authenticated by its own service (it revokes the token it is called with), not by ownership.
TOKEN_ONLY = {("DELETE", "/sessions/current")}

FORBIDDEN = {"code": "forbidden", "detail": "You may only access your own resources."}
INVALID_TOKEN = {
    "code": "invalid_token",
    "detail": "The bearer token is invalid, expired or revoked.",
}
AUTHENTICATION_REQUIRED = {
    "code": "authentication_required",
    "detail": "This request requires a bearer token.",
}
SUCCESS = {"GET": 200, "POST": 201}


@dataclass(frozen=True)
class Customer:
    caller: Caller
    account_id: str
    transaction_id: str


@dataclass(frozen=True)
class World:
    alice: Customer
    bob: Customer


def _key() -> str:
    return f"key-{uuid.uuid7().hex}"


async def _customer(client: AsyncClient, deposit_minor: int) -> Customer:
    caller = await signup(client)
    account = await client.post(
        f"/users/{caller.user_id}/accounts", json={"currency": "GBP"}, headers=caller.headers
    )
    assert account.status_code == 201, account.text
    account_id = account.json()["id"]
    deposit = await client.post(
        f"/users/{caller.user_id}/accounts/{account_id}/deposits",
        json={"amount_minor": deposit_minor},
        headers={**caller.headers, "Idempotency-Key": _key()},
    )
    assert deposit.status_code == 201, deposit.text
    return Customer(caller, account_id, deposit.json()["transaction_id"])


@pytest.fixture
async def world(api_client: AsyncClient) -> World:
    return World(alice=await _customer(api_client, 1_000), bob=await _customer(api_client, 500))


def _url(path: str, owner: Customer) -> str:
    return path.format(
        user_id=owner.caller.user_id,
        account_id=owner.account_id,
        transaction_id=owner.transaction_id,
    )


def _body(path: str, destination: Customer) -> dict[str, Any] | None:
    if path.endswith("/accounts"):
        return {"currency": "EUR"}
    if path.endswith("/transfers"):
        return {"destination_account_id": destination.account_id, "amount_minor": 1}
    if path.endswith(("/deposits", "/withdrawals")):
        return {"amount_minor": 1}
    return None


async def _call(
    client: AsyncClient,
    method: str,
    path: str,
    owner: Customer,
    headers: dict[str, str],
    destination: Customer,
) -> Response:
    """`method path` on `owner`'s ids, as a well-formed request (body and Idempotency-Key)."""
    body = _body(path, destination)
    if method == "POST" and not path.endswith("/accounts"):
        headers = {**headers, "Idempotency-Key": _key()}
    return await client.request(method, _url(path, owner), json=body, headers=headers)


def _routes() -> list[tuple[str, str]]:
    return sorted(PROTECTED)


# --- the inventory ------------------------------------------------------------------------------


async def test_the_route_inventory_is_exactly_known(api_client: AsyncClient) -> None:
    paths = (await api_client.get("/openapi.json")).json()["paths"]
    routes = {(method.upper(), path) for path, operations in paths.items() for method in operations}

    assert routes == PROTECTED | PUBLIC | TOKEN_ONLY
    assert all(path.startswith("/users/{user_id}") for _, path in PROTECTED)


async def test_openapi_declares_bearer_security_exactly_where_it_is_required(
    api_client: AsyncClient,
) -> None:
    paths = (await api_client.get("/openapi.json")).json()["paths"]

    for method, path in PROTECTED | TOKEN_ONLY:
        operation = paths[path][method.lower()]
        assert operation["security"] == [{"HTTPBearer": []}], (method, path)
        assert "401" in operation["responses"], (method, path)
    for method, path in PROTECTED:
        assert "403" in paths[path][method.lower()]["responses"], (method, path)
    for method, path in PUBLIC:
        assert "security" not in paths[path][method.lower()], (method, path)


# --- 401: who is calling ------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), _routes())
async def test_no_token_is_401_before_validation_or_idempotency(
    anonymous_client: AsyncClient, world: World, method: str, path: str
) -> None:
    # No body, no Idempotency-Key: authentication is still decided first (not 422 or 428).
    response = await anonymous_client.request(method, _url(path, world.alice))

    assert (response.status_code, response.json()) == (401, AUTHENTICATION_REQUIRED)
    assert response.headers["www-authenticate"] == "Bearer"


async def _expired_token(db_session: AsyncSession, user_id: str) -> str:
    issued = generate_token()
    await db_session.execute(
        text(
            "INSERT INTO auth_sessions (user_id, token_hash, created_at, expires_at) "
            "VALUES (:u, :h, now() - interval '90 minutes', now() - interval '60 minutes')"
        ),
        {"u": user_id, "h": issued.token_hash},
    )
    await db_session.flush()
    return issued.token


async def _revoked_token(api_client: AsyncClient, caller: Caller) -> str:
    login = await api_client.post(
        "/sessions", json={"email": caller.email, "password": "correct horse battery staple"}
    )
    token: str = login.json()["access_token"]
    logout = await api_client.delete(
        "/sessions/current", headers={"Authorization": f"Bearer {token}"}
    )
    assert logout.status_code == 204
    return token


@pytest.mark.parametrize(("method", "path"), _routes())
async def test_every_kind_of_bad_credential_is_401_on_every_protected_route(
    api_client: AsyncClient, db_session: AsyncSession, world: World, method: str, path: str
) -> None:
    alice = world.alice.caller
    cases = {
        "basic-scheme": ({"Authorization": "Basic YWxpY2U6c2VjcmV0"}, AUTHENTICATION_REQUIRED),
        "scheme-only": ({"Authorization": "Bearer"}, AUTHENTICATION_REQUIRED),
        "malformed": ({"Authorization": "Bearer lt_not-a-real-token"}, INVALID_TOKEN),
        "unknown": ({"Authorization": f"Bearer {generate_token().token}"}, INVALID_TOKEN),
        "expired": (
            {"Authorization": f"Bearer {await _expired_token(db_session, alice.user_id)}"},
            INVALID_TOKEN,
        ),
        "revoked": (
            {"Authorization": f"Bearer {await _revoked_token(api_client, alice)}"},
            INVALID_TOKEN,
        ),
    }

    for name, (headers, expected) in cases.items():
        response = await _call(api_client, method, path, world.alice, headers, world.bob)
        assert (response.status_code, response.json()) == (401, expected), name
        challenge = (
            "Bearer" if expected is AUTHENTICATION_REQUIRED else 'Bearer error="invalid_token"'
        )
        assert response.headers["www-authenticate"] == challenge, name
        assert headers["Authorization"].split(" ", 1)[-1] not in response.text, name


# --- the owner's own token ----------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), _routes())
async def test_the_owners_token_succeeds_on_every_protected_route(
    api_client: AsyncClient, world: World, method: str, path: str
) -> None:
    response = await _call(
        api_client, method, path, world.alice, world.alice.caller.headers, world.bob
    )

    assert response.status_code == SUCCESS[method], response.text


async def test_the_owners_missing_resources_are_still_404(
    api_client: AsyncClient, world: World
) -> None:
    # Authorization does not change what the owner sees of their own scope (ADR 0009).
    alice = world.alice
    headers = alice.caller.headers
    base = f"/users/{alice.caller.user_id}/accounts"

    cases = [
        (f"{base}/{world.bob.account_id}", "account_not_found"),  # someone else's account
        (f"{base}/{uuid.uuid7()}/statement", "account_not_found"),
        (
            f"{base}/{alice.account_id}/transactions/{world.bob.transaction_id}",
            "transaction_not_found",
        ),
    ]
    for url, code in cases:
        response = await api_client.get(url, headers=headers)
        assert (response.status_code, response.json()["code"]) == (404, code), url


# --- 403: whose data ----------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), _routes())
async def test_another_users_token_is_403_on_every_protected_route(
    api_client: AsyncClient, world: World, method: str, path: str
) -> None:
    # Alice's token on Bob's ids: refused before anything is read or written.
    response = await _call(
        api_client, method, path, world.bob, world.alice.caller.headers, world.alice
    )

    assert (response.status_code, response.json()) == (403, FORBIDDEN)


async def _financial_state(db_session: AsyncSession, account_id: str) -> tuple[Any, ...]:
    balance = await db_session.scalar(select(Account.balance_minor).where(Account.id == account_id))
    entries = await db_session.scalar(
        select(func.count()).where(LedgerEntry.account_id == account_id)
    )
    keys = await db_session.scalar(select(func.count()).select_from(IdempotencyKey))
    events = await db_session.scalar(select(func.max(AuditEvent.id)))
    return balance, entries, keys, events


@pytest.mark.parametrize("operation", ["deposits", "withdrawals", "transfers"])
async def test_a_refused_cross_user_posting_changes_nothing(
    api_client: AsyncClient, db_session: AsyncSession, world: World, operation: str
) -> None:
    alice, bob = world.alice, world.bob
    before_bob = await _financial_state(db_session, bob.account_id)
    before_alice = await _financial_state(db_session, alice.account_id)
    key = _key()
    body: dict[str, Any] = {"amount_minor": 100}
    if operation == "transfers":
        body["destination_account_id"] = alice.account_id  # draining Bob into Alice

    response = await api_client.post(
        f"/users/{bob.caller.user_id}/accounts/{bob.account_id}/{operation}",
        json=body,
        headers={**alice.caller.headers, "Idempotency-Key": key},
    )

    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    # Balances, ledger entries, idempotency keys and audit events: all exactly as before.
    assert await _financial_state(db_session, bob.account_id) == before_bob
    assert await _financial_state(db_session, alice.account_id) == before_alice
    assert await db_session.scalar(select(func.count()).where(IdempotencyKey.key == key)) == 0


async def test_another_user_cannot_replay_a_keyed_posting(
    api_client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    alice, bob = world.alice, world.bob
    key = _key()
    original = await api_client.post(
        f"/users/{bob.caller.user_id}/accounts/{bob.account_id}/withdrawals",
        json={"amount_minor": 50},
        headers={**bob.caller.headers, "Idempotency-Key": key},
    )
    assert original.status_code == 201
    before = await _financial_state(db_session, bob.account_id)

    # The exact same request, with Alice's token: refused, no replay, nothing revealed, and
    # nothing written at all (no balance, entry, key or audit change).
    replay = await api_client.post(
        f"/users/{bob.caller.user_id}/accounts/{bob.account_id}/withdrawals",
        json={"amount_minor": 50},
        headers={**alice.caller.headers, "Idempotency-Key": key},
    )
    assert (replay.status_code, replay.json()) == (403, FORBIDDEN)
    assert "idempotent-replayed" not in replay.headers
    assert original.json()["transaction_id"] not in replay.text
    assert await _financial_state(db_session, bob.account_id) == before

    # Bob's key on Alice's own path: Alice's own key scope, and Bob's account is not hers. An
    # authorized request refused by the service: audited as Alice's rejection (ADR 0011),
    # with nothing of Bob's touched.
    own_path = await api_client.post(
        f"/users/{alice.caller.user_id}/accounts/{bob.account_id}/withdrawals",
        json={"amount_minor": 50},
        headers={**alice.caller.headers, "Idempotency-Key": key},
    )
    assert (own_path.status_code, own_path.json()["code"]) == (404, "account_not_found")
    rejection = await db_session.scalar(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1))
    assert rejection is not None
    assert (str(rejection.actor_user_id), rejection.reason) == (
        alice.caller.user_id,
        "account_not_found",
    )
    balance, entries, _, _ = await _financial_state(db_session, bob.account_id)
    assert (balance, entries) == before[:2]
    keys = await db_session.scalars(select(IdempotencyKey.user_id).where(IdempotencyKey.key == key))
    assert [str(user_id) for user_id in keys] == [bob.caller.user_id]  # only Bob's, unchanged


# --- sessions ending between requests -----------------------------------------------------------


async def test_a_session_revoked_between_requests_stops_working(
    api_client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    alice = world.alice.caller
    url = f"/users/{alice.user_id}/accounts"
    assert (await api_client.get(url, headers=alice.headers)).status_code == 200

    await db_session.execute(
        update(AuthSession)
        .where(AuthSession.token_hash == hash_token(alice.token))
        .values(revoked_at=func.now())
    )
    await db_session.flush()

    response = await api_client.get(url, headers=alice.headers)
    assert (response.status_code, response.json()) == (401, INVALID_TOKEN)


async def test_after_logout_the_token_is_refused_everywhere(
    api_client: AsyncClient, world: World
) -> None:
    alice = world.alice

    logout = await api_client.delete("/sessions/current", headers=alice.caller.headers)
    assert logout.status_code == 204

    for method, path in _routes():
        response = await _call(api_client, method, path, alice, alice.caller.headers, world.bob)
        assert (response.status_code, response.json()) == (401, INVALID_TOKEN), (method, path)
    again = await api_client.delete("/sessions/current", headers=alice.caller.headers)
    assert (again.status_code, again.json()) == (401, INVALID_TOKEN)


async def test_public_routes_need_no_token(anonymous_client: AsyncClient) -> None:
    assert (await anonymous_client.get("/health/live")).status_code == 200
    registered = await anonymous_client.post(
        "/users",
        json={
            "email": f"p-{uuid.uuid7().hex}@example.com",
            "password": "correct horse battery staple",
        },
    )
    assert registered.status_code == 201
    login = await anonymous_client.post(
        "/sessions",
        json={"email": registered.json()["email"], "password": "correct horse battery staple"},
    )
    assert login.status_code == 201


# --- no secret anywhere -------------------------------------------------------------------------


async def test_tokens_never_appear_in_responses_logs_or_audit_details(
    api_client: AsyncClient,
    db_session: AsyncSession,
    world: World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    alice, bob = world.alice, world.bob
    since = await db_session.scalar(select(func.max(AuditEvent.id))) or 0
    unknown = generate_token().token
    revoked = await _revoked_token(api_client, alice.caller)
    secrets_ = [alice.caller.token, bob.caller.token, unknown, revoked]

    with caplog.at_level(logging.DEBUG):
        responses = [
            await _call(api_client, method, path, bob, alice.caller.headers, alice)  # 403
            for method, path in _routes()
        ] + [
            await _call(api_client, method, path, alice, {"Authorization": f"Bearer {token}"}, bob)
            for method, path in _routes()
            for token in (unknown, revoked)  # 401
        ]

    details = (
        await db_session.scalars(select(AuditEvent.details).where(AuditEvent.id > since))
    ).all()
    texts = [response.text for response in responses] + [caplog.text, str(details)]
    for secret in secrets_:
        for text_ in texts:
            assert secret not in text_
            assert hash_token(secret).hex() not in text_
