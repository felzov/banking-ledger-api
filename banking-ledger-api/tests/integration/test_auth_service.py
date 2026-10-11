"""Registration with a password, login and logout (ADR 0013), with real COMMITs on the
disposable test database.

Argon2id runs with the cheapest parameters (the protocol is under test, not Argon2's cost).
Every test checks the persisted rows, and that no secret reaches the audit trail or the logs.
"""

import asyncio
import logging
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import pytest
from argon2 import PasswordHasher
from sqlalchemy import URL, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import QueuePool

from ledger_api.data.audit import insert_audit_event
from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.models import AuditEvent, AuthSession, User, UserCredential
from ledger_api.domain.audit import AuditAction, AuditOutcome, AuditRecord
from ledger_api.domain.errors import (
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidTokenError,
)
from ledger_api.domain.passwords import (
    Argon2Parameters,
    InvalidPasswordError,
    hash_password,
    make_hasher,
    verify_password,
)
from ledger_api.domain.tokens import generate_token, hash_token, is_well_formed_token
from ledger_api.services import audit as audit_service
from ledger_api.services import auth as auth_service
from ledger_api.services import users as users_service
from ledger_api.services.auth import LoginResult, login, logout
from ledger_api.services.users import register_user, register_user_with_password

type Maker = async_sessionmaker[AsyncSession]

CHEAP = Argon2Parameters(time_cost=1, memory_cost=8, parallelism=1)
PHRASE = "correct horse battery staple"
LIFETIME = timedelta(minutes=30)
AUTH_LOGGERS = ("ledger_api",)


class InjectedFailureError(Exception):
    pass


@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    return make_hasher(CHEAP)


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> Maker:
    return create_sessionmaker(engine)


def _email() -> str:
    return f"auth-{uuid.uuid7().hex}@example.test"


async def _register(sm: Maker, hasher: PasswordHasher, email: str) -> User:
    async with sm() as session:
        return await register_user_with_password(session, hasher, email=email, password=PHRASE)


async def _login(
    sm: Maker, hasher: PasswordHasher, email: str, password: str = PHRASE
) -> LoginResult:
    async with sm() as session:
        return await login(session, hasher, email=email, password=password, lifetime=LIFETIME)


async def _logout(sm: Maker, token: str) -> None:
    async with sm() as session:
        await logout(session, token=token)


async def _credential(sm: Maker, user_id: uuid.UUID) -> UserCredential | None:
    async with sm() as session:
        return await session.get(UserCredential, user_id)


async def _sessions_of(sm: Maker, user_id: uuid.UUID) -> list[AuthSession]:
    async with sm() as session:
        rows = await session.scalars(select(AuthSession).where(AuthSession.user_id == user_id))
        return list(rows.all())


async def _auth_events(sm: Maker, *, actor: uuid.UUID | None, since: int) -> list[AuditEvent]:
    async with sm() as session:
        statement = (
            select(AuditEvent)
            .where(
                AuditEvent.id > since,
                AuditEvent.action.in_([AuditAction.AUTH_LOGIN, AuditAction.AUTH_LOGOUT]),
            )
            .order_by(AuditEvent.id)
        )
        statement = statement.where(
            AuditEvent.actor_user_id.is_(None)
            if actor is None
            else AuditEvent.actor_user_id == actor
        )
        return list((await session.scalars(statement)).all())


async def _latest_event_id(sm: Maker) -> int:
    async with sm() as session:
        return await session.scalar(select(func.max(AuditEvent.id))) or 0


def _assert_no_secret(texts: list[str], *secrets: str) -> None:
    for text_ in texts:
        for secret in secrets:
            assert secret not in text_


# --- registration -------------------------------------------------------------------------------


async def test_registration_stores_an_argon2id_credential_with_the_user(
    sessionmaker: Maker, hasher: PasswordHasher
) -> None:
    email = _email()

    user = await _register(sessionmaker, hasher, email)

    credential = await _credential(sessionmaker, user.id)
    assert credential is not None
    assert credential.password_hash.startswith("$argon2id$")
    assert verify_password(hasher, credential.password_hash, PHRASE)
    async with sessionmaker() as session:
        event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.USER_REGISTER, AuditEvent.actor_user_id == user.id
            )
        )
    assert event is not None
    assert event.outcome == AuditOutcome.SUCCEEDED
    _assert_no_secret([str(event.details)], PHRASE, credential.password_hash, email)


async def test_a_duplicate_email_registers_nothing(
    sessionmaker: Maker, hasher: PasswordHasher
) -> None:
    email = _email()
    first = await _register(sessionmaker, hasher, email)

    with pytest.raises(EmailAlreadyRegisteredError):
        await _register(sessionmaker, hasher, email)

    async with sessionmaker() as session:
        users = await session.scalar(select(func.count()).where(User.email == email))
        credentials = await session.scalar(
            select(func.count()).select_from(UserCredential).join(User).where(User.email == email)
        )
    assert (users, credentials) == (1, 1)
    assert (await _credential(sessionmaker, first.id)) is not None


@pytest.mark.parametrize("password", ["too short", "x" * 129, "   "])
async def test_a_policy_violating_password_registers_nothing(
    sessionmaker: Maker, hasher: PasswordHasher, password: str
) -> None:
    email = _email()
    since = await _latest_event_id(sessionmaker)

    with pytest.raises(InvalidPasswordError):
        async with sessionmaker() as session:
            await register_user_with_password(session, hasher, email=email, password=password)

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).where(User.email == email)) == 0
        # Refused before any transaction: not even an audit event.
        assert await session.scalar(select(func.count()).where(AuditEvent.id > since)) == 0


async def test_a_failed_registration_leaves_no_user_or_credential(
    sessionmaker: Maker, hasher: PasswordHasher, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = _email()

    async def failing_success_write(session: AsyncSession, record: AuditRecord) -> None:
        if record.outcome == AuditOutcome.SUCCEEDED:
            raise InjectedFailureError
        await insert_audit_event(session, record)

    monkeypatch.setattr(audit_service, "insert_audit_event", failing_success_write)

    with pytest.raises(InjectedFailureError):
        await _register(sessionmaker, hasher, email)

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).where(User.email == email)) == 0
        orphans = await session.scalar(
            select(func.count())
            .select_from(UserCredential)
            .outerjoin(User, User.id == UserCredential.user_id)
            .where(User.id.is_(None))
        )
    assert orphans == 0


@pytest.mark.concurrency
async def test_concurrent_registrations_of_one_email_create_exactly_one_user(
    sessionmaker: Maker, hasher: PasswordHasher
) -> None:
    email = _email()
    start = asyncio.Event()

    async def attempt() -> User:
        async with sessionmaker() as session:
            await start.wait()
            return await register_user_with_password(session, hasher, email=email, password=PHRASE)

    tasks = [asyncio.create_task(attempt()) for _ in range(5)]
    await asyncio.sleep(0)
    start.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    winners = [result for result in results if isinstance(result, User)]
    assert len(winners) == 1
    assert all(
        isinstance(result, EmailAlreadyRegisteredError)
        for result in results
        if result not in winners
    )
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).where(User.email == email)) == 1
    assert (await _credential(sessionmaker, winners[0].id)) is not None


# --- login --------------------------------------------------------------------------------------


async def test_login_opens_a_session_storing_only_the_token_digest_in_database_time(
    sessionmaker: Maker, hasher: PasswordHasher
) -> None:
    email = _email()
    user = await _register(sessionmaker, hasher, email)
    since = await _latest_event_id(sessionmaker)

    result = await _login(sessionmaker, hasher, email)

    assert is_well_formed_token(result.token)
    assert result.user_id == user.id
    [stored] = await _sessions_of(sessionmaker, user.id)
    assert stored.id == result.session_id
    assert stored.token_hash == hash_token(result.token)
    assert result.token.encode() not in stored.token_hash
    assert stored.expires_at == result.expires_at
    assert stored.expires_at - stored.created_at == LIFETIME
    assert stored.revoked_at is None
    [event] = await _auth_events(sessionmaker, actor=user.id, since=since)
    assert (event.action, event.outcome) == (AuditAction.AUTH_LOGIN, AuditOutcome.SUCCEEDED)
    assert event.details == {"session_id": str(result.session_id)}
    _assert_no_secret(
        [str(event.details), repr(result)],
        result.token,
        stored.token_hash.hex(),
        PHRASE,
        email,
    )


async def test_the_session_starts_at_the_databases_now(
    sessionmaker: Maker, hasher: PasswordHasher
) -> None:
    email = _email()
    user = await _register(sessionmaker, hasher, email)
    async with sessionmaker() as session:
        before = await session.scalar(select(func.now()))

    await _login(sessionmaker, hasher, email)

    async with sessionmaker() as session:
        after = await session.scalar(select(func.now()))
    [stored] = await _sessions_of(sessionmaker, user.id)
    assert before is not None
    assert after is not None
    assert before <= stored.created_at <= after


async def test_wrong_password_and_unknown_email_fail_identically(
    sessionmaker: Maker, hasher: PasswordHasher
) -> None:
    email = _email()
    user = await _register(sessionmaker, hasher, email)
    since = await _latest_event_id(sessionmaker)

    with pytest.raises(InvalidCredentialsError) as wrong:
        await _login(sessionmaker, hasher, email, "wrong horse battery staple")
    with pytest.raises(InvalidCredentialsError) as unknown:
        await _login(sessionmaker, hasher, _email())

    assert (wrong.value.code, str(wrong.value)) == (unknown.value.code, str(unknown.value))
    assert await _sessions_of(sessionmaker, user.id) == []
    # Audited (internally) as rejected: the targeted user when the email exists, else none.
    [targeted] = await _auth_events(sessionmaker, actor=user.id, since=since)
    anonymous = await _auth_events(sessionmaker, actor=None, since=since)
    assert (targeted.outcome, targeted.reason) == (AuditOutcome.REJECTED, "invalid_credentials")
    assert [(e.outcome, e.reason) for e in anonymous] == [
        (AuditOutcome.REJECTED, "invalid_credentials")
    ]
    _assert_no_secret(
        [str(targeted.details), str(anonymous[0].details)],
        PHRASE,
        "wrong horse battery staple",
        email,
    )


async def test_every_login_failure_pays_for_exactly_one_argon2_verification(
    sessionmaker: Maker, hasher: PasswordHasher, monkeypatch: pytest.MonkeyPatch
) -> None:
    email_with_password = _email()
    await _register(sessionmaker, hasher, email_with_password)
    email_without_password = _email()
    async with sessionmaker() as session:  # a user from before Phase 8: no credential
        await register_user(session, email=email_without_password)
    verifications: list[str] = []
    real_verify = verify_password

    def counting_verify(hasher_: PasswordHasher, stored: str, password: str) -> bool:
        verifications.append(stored)
        return real_verify(hasher_, stored, password)

    monkeypatch.setattr(auth_service, "verify_password", counting_verify)

    for email in (email_with_password, email_without_password, _email()):
        verifications.clear()
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, hasher, email, "wrong horse battery staple")
        assert len(verifications) == 1, email
        assert verifications[0].startswith("$argon2id$")


async def test_a_user_without_a_password_cannot_log_in(
    sessionmaker: Maker, hasher: PasswordHasher
) -> None:
    email = _email()
    async with sessionmaker() as session:
        user = await register_user(session, email=email)

    with pytest.raises(InvalidCredentialsError):
        await _login(sessionmaker, hasher, email)

    assert await _sessions_of(sessionmaker, user.id) == []


async def test_a_successful_login_rehashes_a_password_made_with_older_parameters(
    sessionmaker: Maker, hasher: PasswordHasher
) -> None:
    email = _email()
    user = await _register(sessionmaker, hasher, email)
    stronger = make_hasher(Argon2Parameters(time_cost=2, memory_cost=16, parallelism=1))

    await _login(sessionmaker, stronger, email)

    credential = await _credential(sessionmaker, user.id)
    assert credential is not None
    assert "$m=16,t=2,p=1$" in credential.password_hash
    assert credential.updated_at >= credential.created_at
    await _login(sessionmaker, stronger, email)  # still the same password


async def test_a_failed_login_write_leaves_no_session(
    sessionmaker: Maker, hasher: PasswordHasher, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = _email()
    user = await _register(sessionmaker, hasher, email)
    since = await _latest_event_id(sessionmaker)

    async def failing_success_write(session: AsyncSession, record: AuditRecord) -> None:
        if record.outcome == AuditOutcome.SUCCEEDED:
            raise InjectedFailureError
        await insert_audit_event(session, record)

    monkeypatch.setattr(audit_service, "insert_audit_event", failing_success_write)

    with pytest.raises(InjectedFailureError):
        await _login(sessionmaker, hasher, email)

    assert await _sessions_of(sessionmaker, user.id) == []  # rolled back with the audit event
    [failed] = await _auth_events(sessionmaker, actor=user.id, since=since)
    assert (failed.outcome, failed.reason) == (AuditOutcome.FAILED, "internal_error")


# --- logout -------------------------------------------------------------------------------------


async def test_logout_revokes_the_session_once_and_is_audited(
    sessionmaker: Maker, hasher: PasswordHasher
) -> None:
    email = _email()
    user = await _register(sessionmaker, hasher, email)
    result = await _login(sessionmaker, hasher, email)
    since = await _latest_event_id(sessionmaker)

    await _logout(sessionmaker, result.token)

    [stored] = await _sessions_of(sessionmaker, user.id)
    assert stored.revoked_at is not None
    assert stored.revoked_at >= stored.created_at
    [event] = await _auth_events(sessionmaker, actor=user.id, since=since)
    assert (event.action, event.outcome) == (AuditAction.AUTH_LOGOUT, AuditOutcome.SUCCEEDED)
    assert event.details == {"session_id": str(result.session_id)}

    # Again with the same token: invalid, not audited, and the revocation is unchanged.
    with pytest.raises(InvalidTokenError):
        await _logout(sessionmaker, result.token)
    [unchanged] = await _sessions_of(sessionmaker, user.id)
    assert unchanged.revoked_at == stored.revoked_at
    assert len(await _auth_events(sessionmaker, actor=user.id, since=since)) == 1


@pytest.mark.parametrize(
    "token",
    [generate_token().token, "lt_short", "not a token", ""],
    ids=["unknown", "malformed", "garbage", "empty"],
)
async def test_logout_with_an_invalid_token_changes_nothing(
    sessionmaker: Maker, token: str
) -> None:
    since = await _latest_event_id(sessionmaker)

    with pytest.raises(InvalidTokenError):
        await _logout(sessionmaker, token)

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).where(AuditEvent.id > since)) == 0


async def test_an_expired_session_cannot_be_logged_out(
    sessionmaker: Maker, hasher: PasswordHasher
) -> None:
    user = await _register(sessionmaker, hasher, _email())
    issued = generate_token()
    async with sessionmaker() as session, session.begin():
        # Expired one hour ago by the database's clock.
        await session.execute(
            text(
                "INSERT INTO auth_sessions (user_id, token_hash, created_at, expires_at) "
                "VALUES (:u, :h, now() - interval '90 minutes', now() - interval '60 minutes')"
            ),
            {"u": user.id, "h": issued.token_hash},
        )

    with pytest.raises(InvalidTokenError):
        await _logout(sessionmaker, issued.token)

    [stored] = await _sessions_of(sessionmaker, user.id)
    assert stored.revoked_at is None  # untouched: the trigger was never reached


@pytest.mark.concurrency
async def test_concurrent_logouts_revoke_once(sessionmaker: Maker, hasher: PasswordHasher) -> None:
    email = _email()
    user = await _register(sessionmaker, hasher, email)
    result = await _login(sessionmaker, hasher, email)
    since = await _latest_event_id(sessionmaker)
    start = asyncio.Event()

    async def attempt() -> None:
        async with sessionmaker() as session:
            await start.wait()
            await logout(session, token=result.token)

    tasks = [asyncio.create_task(attempt()) for _ in range(5)]
    await asyncio.sleep(0)
    start.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    assert sum(outcome is None for outcome in outcomes) == 1
    assert all(isinstance(o, InvalidTokenError) for o in outcomes if o is not None)
    assert len(await _auth_events(sessionmaker, actor=user.id, since=since)) == 1


# --- no connection during Argon2, no secret in logs ---------------------------------------------


@pytest.fixture
async def single_connection_engine(
    engine: AsyncEngine, test_database_url: URL
) -> AsyncIterator[AsyncEngine]:
    single = create_async_engine(test_database_url, pool_size=1, max_overflow=0, pool_timeout=1)
    try:
        yield single
    finally:
        await single.dispose()


async def test_argon2_runs_in_a_worker_thread_without_a_database_connection(
    single_connection_engine: AsyncEngine,
    hasher: PasswordHasher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessionmaker = create_sessionmaker(single_connection_engine)
    pool = single_connection_engine.pool
    assert isinstance(pool, QueuePool)
    loop_thread = threading.current_thread()
    observed: list[tuple[str, int, bool]] = []

    def watching(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            observed.append((name, pool.checkedout(), threading.current_thread() is loop_thread))
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(users_service, "hash_password", watching("register", hash_password))
    monkeypatch.setattr(auth_service, "verify_password", watching("verify", verify_password))
    email = _email()

    await _register(sessionmaker, hasher, email)
    await _login(sessionmaker, hasher, email)  # stored hash
    with pytest.raises(InvalidCredentialsError):
        await _login(sessionmaker, hasher, _email())  # dummy hash

    assert [name for name, _, _ in observed] == ["register", "verify", "verify"]
    # Never a connection checked out, never on the event loop's thread.
    assert all(checked_out == 0 and not on_loop for _, checked_out, on_loop in observed)


async def test_no_password_token_or_digest_is_ever_logged(
    sessionmaker: Maker, hasher: PasswordHasher, caplog: pytest.LogCaptureFixture
) -> None:
    email = _email()
    with caplog.at_level(logging.DEBUG):
        await _register(sessionmaker, hasher, email)
        result = await _login(sessionmaker, hasher, email)
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, hasher, email, "wrong horse battery staple")
        await _logout(sessionmaker, result.token)
        with pytest.raises(InvalidTokenError):
            await _logout(sessionmaker, result.token)

    _assert_no_secret(
        [caplog.text],
        PHRASE,
        "wrong horse battery staple",
        result.token,
        hash_token(result.token).hex(),
    )
