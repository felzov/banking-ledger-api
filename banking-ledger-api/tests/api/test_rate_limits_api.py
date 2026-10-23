"""Rate limits over HTTP (ADR 0016): a real Redis, and the test's rolled-back database.

Each test has its own key namespace, so counters never leak between tests; client addresses are
set per client (httpx's ASGITransport reports them to the app as the peer).
"""

import asyncio
import io
import json
import logging
import threading
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import URL, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_api.api.dependencies import get_session
from ledger_api.data.models import AuditEvent, LedgerEntry
from ledger_api.domain import passwords
from ledger_api.main import create_app
from ledger_api.observability.logs import configure_logging
from tests.auth_helpers import TEST_PASSWORD, Caller, signup
from tests.conftest import SessionFactory, settings_for

RATE_LIMITED = "rate_limited"
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"

type ClientAt = Callable[[str], AsyncClient]


@pytest.fixture
def make_app(
    engine: AsyncEngine,  # the test database exists and is migrated
    test_database_url: URL,
    new_session: SessionFactory,
    test_redis_url: str,
    rate_limit_namespace: str,
) -> Callable[..., FastAPI]:
    """An app with rate limits (the settings given), whose requests run on the test's connection."""

    def make(**settings: Any) -> FastAPI:
        app = create_app(
            settings_for(
                test_database_url,
                redis_url=SecretStr(settings.pop("redis_url", test_redis_url)),
                rate_limit_namespace=rate_limit_namespace,
                **settings,
            ),
            privilege_guard=False,  # the suite's superuser connection
        )

        async def session_on_test_connection() -> AsyncIterator[AsyncSession]:
            async with new_session() as session:
                yield session

        app.dependency_overrides[get_session] = session_on_test_connection
        return app

    return make


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[ClientAt]:
    """The app's lifespan, and clients that each come from an address of their own."""
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))

        def at(address: str) -> AsyncClient:
            client = AsyncClient(
                transport=ASGITransport(app=app, client=(address, 50_000)),
                base_url="http://testserver",
            )
            stack.push_async_callback(client.aclose)
            return client

        yield at


@pytest.fixture
def log_lines() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    yield buffer
    root = logging.getLogger()
    for handler in [h for h in root.handlers if h.get_name() == "ledger_api.json"]:
        root.removeHandler(handler)


def _email() -> str:
    return f"rl-{uuid.uuid7().hex}@example.com"


async def _login(
    client: AsyncClient,
    email: str,
    password: str = "wrong-password-1",  # noqa: S107 (synthetic test data)
) -> Response:
    return await client.post("/sessions", json={"email": email, "password": password})


def _assert_rate_limited(response: Response, window_seconds: int) -> None:
    assert response.status_code == 429, response.text
    assert response.json()["code"] == RATE_LIMITED
    assert 1 <= int(response.headers["retry-after"]) <= window_seconds


async def _audit_events_of(db_session: AsyncSession, response: Response) -> int:
    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.request_id == response.headers["x-request-id"])
    )
    return int(count or 0)


# --- login: per client address -----------------------------------------------------------------


async def test_login_attempts_are_limited_per_address(make_app: Callable[..., FastAPI]) -> None:
    async with serving(make_app(login_attempts_per_minute=3)) as at:
        client = at("203.0.113.1")
        statuses = [(await _login(client, _email())).status_code for _ in range(3)]
        refused = await _login(client, _email())
        other_address = await _login(at("203.0.113.2"), _email())

    assert statuses == [401, 401, 401]
    _assert_rate_limited(refused, 60)
    assert other_address.status_code == 401


async def test_malformed_login_attempts_count_too(make_app: Callable[..., FastAPI]) -> None:
    # The limit is a dependency: it runs before the body is validated.
    async with serving(make_app(login_attempts_per_minute=2)) as at:
        client = at("203.0.113.3")
        malformed = [(await client.post("/sessions", json={})).status_code for _ in range(2)]
        refused = await _login(client, _email())

    assert malformed == [422, 422]
    _assert_rate_limited(refused, 60)


async def test_ipv6_clients_are_limited_per_64(make_app: Callable[..., FastAPI]) -> None:
    async with serving(make_app(login_attempts_per_minute=1)) as at:
        first = await _login(at("2001:db8:1:2::1"), _email())
        same_64 = await _login(at("2001:db8:1:2::ffff"), _email())
        other_64 = await _login(at("2001:db8:1:3::1"), _email())

    assert first.status_code == 401
    _assert_rate_limited(same_64, 60)
    assert other_64.status_code == 401


# --- login: per email's failed attempts ----------------------------------------------------------


async def test_failed_logins_throttle_an_email_registered_or_not(
    make_app: Callable[..., FastAPI],
) -> None:
    async with serving(make_app(login_failures_per_email=3)) as at:
        client = at("203.0.113.4")
        registered = (await signup(client)).email
        unknown = _email()
        known_answers = [await _login(client, registered) for _ in range(4)]
        unknown_answers = [await _login(client, unknown) for _ in range(4)]
        # The right password, during the throttle: refused too, before any check.
        right_password = await _login(client, registered, TEST_PASSWORD)

    assert [r.status_code for r in known_answers] == [401, 401, 401, 429]
    assert [r.status_code for r in unknown_answers] == [401, 401, 401, 429]
    # Nothing tells a registered email from an unknown one.
    assert known_answers[-1].json() == unknown_answers[-1].json()
    _assert_rate_limited(right_password, 900)


async def test_a_successful_login_clears_the_failed_attempts(
    make_app: Callable[..., FastAPI],
) -> None:
    async with serving(make_app(login_failures_per_email=3)) as at:
        client = at("203.0.113.5")
        email = (await signup(client)).email
        before = [(await _login(client, email)).status_code for _ in range(2)]
        success = await _login(client, email, TEST_PASSWORD)
        after = [(await _login(client, email)).status_code for _ in range(4)]

    assert before == [401, 401]
    assert success.status_code == 201
    assert after == [401, 401, 401, 429]  # counted from zero again


async def test_a_throttled_login_is_not_audited_and_computes_no_hash(
    make_app: Callable[..., FastAPI],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifications = 0
    real = passwords.verify_password

    def counting(*args: Any) -> bool:
        nonlocal verifications
        verifications += 1
        return real(*args)

    # services.auth calls the name it imported: that is the one to count.
    monkeypatch.setattr("ledger_api.services.auth.verify_password", counting)
    async with serving(make_app(login_failures_per_email=1)) as at:
        client = at("203.0.113.6")
        email = (await signup(client)).email
        failed = await _login(client, email)
        hashes_before = verifications
        refused = await _login(client, email)

    assert failed.status_code == 401
    _assert_rate_limited(refused, 900)
    assert verifications == hashes_before  # refused before Argon2
    assert await _audit_events_of(db_session, failed) == 1
    assert await _audit_events_of(db_session, refused) == 0


async def test_concurrent_logins_cannot_outrun_the_email_limit(
    engine: AsyncEngine,
    test_database_url: URL,
    test_redis_url: str,
    rate_limit_namespace: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review fix (B1): the email was checked before the password and the failure counted after
    # it, so every attempt in flight passed the check: 40 concurrent guesses, all checked, against
    # a limit of 3. Concurrent requests cannot share the suite's one rolled-back connection, so
    # this app uses its own pool (its rejected-login audit events are committed).
    verifications = 0
    lock = threading.Lock()
    real = passwords.verify_password

    def counting(*args: Any) -> bool:
        nonlocal verifications
        with lock:  # Argon2 runs in worker threads
            verifications += 1
        return real(*args)

    monkeypatch.setattr("ledger_api.services.auth.verify_password", counting)
    app = create_app(
        settings_for(
            test_database_url,
            redis_url=SecretStr(test_redis_url),
            rate_limit_namespace=rate_limit_namespace,
            login_failures_per_email=3,
            login_attempts_per_minute=1_000,
        ),
        privilege_guard=False,  # the suite's superuser connection
    )
    email = _email()
    async with serving(app) as at:
        clients = [at(f"203.0.113.{i}") for i in range(1, 41)]
        answers = await asyncio.gather(*(_login(client, email) for client in clients))

    assert Counter(r.status_code for r in answers) == {401: 3, 429: 37}
    assert verifications == 3


# --- registration ------------------------------------------------------------------------------


async def test_registrations_are_limited_per_address(make_app: Callable[..., FastAPI]) -> None:
    async with serving(make_app(registrations_per_hour=2)) as at:
        client = at("203.0.113.7")
        email = _email()
        credentials = {"email": email, "password": TEST_PASSWORD}
        first = await client.post("/users", json=credentials)
        duplicate = await client.post("/users", json=credentials)  # counts too: enumeration
        refused = await client.post("/users", json={**credentials, "email": _email()})
        other_address = await at("203.0.113.8").post(
            "/users", json={**credentials, "email": _email()}
        )

    assert (first.status_code, duplicate.status_code) == (201, 409)
    _assert_rate_limited(refused, 3600)
    assert other_address.status_code == 201


# --- postings ------------------------------------------------------------------------------------


async def _account(client: AsyncClient, caller: Caller) -> str:
    opened = await client.post(
        f"/users/{caller.user_id}/accounts", json={"currency": "GBP"}, headers=caller.headers
    )
    assert opened.status_code == 201, opened.text
    account_id: str = opened.json()["id"]
    return account_id


async def _post(client: AsyncClient, caller: Caller, account: str, kind: str, key: str) -> Response:
    return await client.post(
        f"/users/{caller.user_id}/accounts/{account}/{kind}",
        json={"amount_minor": 100},
        headers={**caller.headers, "Idempotency-Key": key},
    )


async def test_postings_are_limited_per_user_replays_and_kinds_together(
    make_app: Callable[..., FastAPI], db_session: AsyncSession
) -> None:
    async with serving(make_app(postings_per_minute=2)) as at:
        client = at("203.0.113.9")
        alice, bob = await signup(client), await signup(client)
        alice_account, bob_account = await _account(client, alice), await _account(client, bob)
        deposit = await _post(client, alice, alice_account, "deposits", "k-1")
        replay = await _post(client, alice, alice_account, "deposits", "k-1")
        refused = await _post(client, alice, alice_account, "withdrawals", "k-2")
        bob_deposit = await _post(client, bob, bob_account, "deposits", "k-1")

    assert (deposit.status_code, replay.status_code) == (201, 201)
    assert replay.headers["idempotent-replayed"] == "true"
    _assert_rate_limited(refused, 60)
    assert bob_deposit.status_code == 201  # per user
    # The refused withdrawal moved nothing and wrote no audit event.
    entries = await db_session.scalar(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.account_id == uuid.UUID(alice_account))
    )
    assert entries == 1
    assert await _audit_events_of(db_session, refused) == 0


# --- Redis unavailable ---------------------------------------------------------------------------


async def test_without_redis_login_and_registration_are_refused(
    make_app: Callable[..., FastAPI],
) -> None:
    # Fail closed: these limits are the defence against password guessing and enumeration.
    async with serving(make_app(redis_url=UNREACHABLE_REDIS_URL)) as at:
        client = at("203.0.113.10")
        login = await _login(client, _email())
        registration = await client.post(
            "/users", json={"email": _email(), "password": TEST_PASSWORD}
        )

    for response in (login, registration):
        assert response.status_code == 503
        assert response.json()["code"] == "temporarily_unavailable"


async def test_without_redis_postings_still_move_money_and_say_so_in_the_log(
    make_app: Callable[..., FastAPI], log_lines: io.StringIO
) -> None:
    # Fail open: this limit only bounds an authenticated user's load. The user and account come
    # from an app whose Redis works; both apps run on the test's connection.
    async with serving(make_app()) as at:
        client = at("203.0.113.11")
        caller = await signup(client)
        account = await _account(client, caller)
    async with serving(make_app(redis_url=UNREACHABLE_REDIS_URL)) as at:
        configure_logging("INFO", stream=log_lines)  # after the lifespan sent it to stderr
        deposit = await _post(at("203.0.113.11"), caller, account, "deposits", "k-1")

    assert deposit.status_code == 201, deposit.text
    entries = [json.loads(line) for line in log_lines.getvalue().splitlines()]
    (warning,) = [e for e in entries if e.get("rate_limit") == "postings_user"]
    assert warning["level"] == "WARNING"
    assert "not enforced" in warning["message"]
    assert warning["request_id"] == deposit.headers["x-request-id"]
