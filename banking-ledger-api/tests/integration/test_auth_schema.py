"""user_credentials, auth_sessions and the authentication audit actions (ADR 0013, migration
0012).

Every test runs on the rolled-back test connection: nothing is committed.
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.audit import insert_audit_event
from ledger_api.data.models import AuditEvent, AuthSession, UserCredential
from ledger_api.domain.audit import AuditAction, AuditOutcome, AuditRecord
from ledger_api.domain.passwords import Argon2Parameters, hash_password, make_hasher
from ledger_api.domain.tokens import generate_token
from tests.database import raises_violation
from tests.factories import create_user

CHEAP = make_hasher(Argon2Parameters(time_cost=1, memory_cost=8, parallelism=1))
PHRASE = "correct horse battery staple"


async def _credential(session: AsyncSession, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"password_hash": hash_password(CHEAP, PHRASE)}
    if "user_id" not in overrides:
        values["user_id"] = (await create_user(session)).id
    values.update(overrides)
    await session.execute(insert(UserCredential).values(**values))
    return values


async def _session_row(session: AsyncSession, **overrides: Any) -> uuid.UUID:
    """An auth session expiring 30 minutes after the transaction's start (database time)."""
    values: dict[str, Any] = {
        "token_hash": generate_token().token_hash,
        "expires_at": func.now() + timedelta(minutes=30),
    }
    if "user_id" not in overrides:
        values["user_id"] = (await create_user(session)).id
    values.update(overrides)
    session_id = await session.scalar(
        insert(AuthSession).values(**values).returning(AuthSession.id)
    )
    assert isinstance(session_id, uuid.UUID)
    return session_id


# --- user_credentials ---------------------------------------------------------------------------


async def test_an_argon2id_hash_from_the_domain_is_stored(db_session: AsyncSession) -> None:
    values = await _credential(db_session)

    row = await db_session.get(UserCredential, values["user_id"])

    assert row is not None
    assert row.password_hash == values["password_hash"]
    assert row.created_at is not None
    assert row.updated_at == row.created_at
    assert PHRASE not in row.password_hash
    assert row.password_hash not in repr(row)


@pytest.mark.parametrize(
    "password_hash",
    [
        PHRASE,  # plaintext
        "$argon2i$v=19$m=8,t=1,p=1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo",  # another Argon2 type
        "$2b$12$abcdefghijklmnopqrstuuabcdefghijklmnopqrstuvwxyz012",  # bcrypt
        "",
        "$argon2id$" + "a" * 503,  # longer than 512 characters
    ],
    ids=["plaintext", "argon2i", "bcrypt", "empty", "too-long"],
)
async def test_only_bounded_argon2id_hashes_are_accepted(
    db_session: AsyncSession, password_hash: str
) -> None:
    async with raises_violation("ck_user_credentials_password_hash_argon2id"):
        await _credential(db_session, password_hash=password_hash)


async def test_a_user_has_at_most_one_credential(db_session: AsyncSession) -> None:
    values = await _credential(db_session)

    async with raises_violation("pk_user_credentials"):
        await _credential(db_session, user_id=values["user_id"])


async def test_a_credential_belongs_to_an_existing_user(db_session: AsyncSession) -> None:
    async with raises_violation("fk_user_credentials_user_id_users"):
        await _credential(db_session, user_id=uuid.uuid7())


async def test_a_credential_cannot_be_updated_before_it_was_created(
    db_session: AsyncSession,
) -> None:
    async with raises_violation("ck_user_credentials_updated_after_created"):
        await _credential(db_session, updated_at=func.now() - timedelta(seconds=1))


async def test_a_credential_can_be_rehashed(db_session: AsyncSession) -> None:
    values = await _credential(db_session)
    rehashed = hash_password(CHEAP, PHRASE)

    await db_session.execute(
        update(UserCredential)
        .where(UserCredential.user_id == values["user_id"])
        .values(password_hash=rehashed, updated_at=func.now())
    )

    stored = await db_session.scalar(
        select(UserCredential.password_hash).where(UserCredential.user_id == values["user_id"])
    )
    assert stored == rehashed


# --- auth_sessions ------------------------------------------------------------------------------


async def test_a_session_stores_only_the_token_digest_with_database_times(
    db_session: AsyncSession,
) -> None:
    issued = generate_token()
    session_id = await _session_row(db_session, token_hash=issued.token_hash)

    row = await db_session.get(AuthSession, session_id)
    now = await db_session.scalar(select(func.now()))

    assert row is not None
    assert row.token_hash == issued.token_hash
    assert row.created_at == now  # database time: the transaction's start
    assert row.expires_at - row.created_at == timedelta(minutes=30)
    assert row.revoked_at is None
    assert issued.token.encode() not in row.token_hash
    assert issued.token_hash.hex() not in repr(row)


@pytest.mark.parametrize("length", [0, 31, 33, 64])
async def test_the_token_digest_is_exactly_32_bytes(db_session: AsyncSession, length: int) -> None:
    async with raises_violation("ck_auth_sessions_token_hash_length"):
        await _session_row(db_session, token_hash=b"\x01" * length)


async def test_a_token_digest_is_unique(db_session: AsyncSession) -> None:
    issued = generate_token()
    await _session_row(db_session, token_hash=issued.token_hash)

    async with raises_violation("uq_auth_sessions_token_hash"):
        await _session_row(db_session, token_hash=issued.token_hash)


async def test_a_session_belongs_to_an_existing_user(db_session: AsyncSession) -> None:
    async with raises_violation("fk_auth_sessions_user_id_users"):
        await _session_row(db_session, user_id=uuid.uuid7())


@pytest.mark.parametrize(
    "expires_in",
    [timedelta(0), -timedelta(minutes=1), timedelta(hours=24, microseconds=1), timedelta(days=30)],
    ids=["at-creation", "before-creation", "just-over-24h", "30-days"],
)
async def test_a_session_expires_after_creation_and_within_24_hours(
    db_session: AsyncSession, expires_in: timedelta
) -> None:
    async with raises_violation("ck_auth_sessions_expiry_window"):
        await _session_row(db_session, expires_at=func.now() + expires_in)


async def test_a_24_hour_session_is_the_longest_accepted(db_session: AsyncSession) -> None:
    await _session_row(db_session, expires_at=func.now() + timedelta(hours=24))


async def test_a_session_cannot_be_revoked_before_it_was_created(
    db_session: AsyncSession,
) -> None:
    async with raises_violation("ck_auth_sessions_revoked_after_created"):
        await _session_row(db_session, revoked_at=func.now() - timedelta(seconds=1))


async def test_a_session_can_be_revoked_once(db_session: AsyncSession) -> None:
    session_id = await _session_row(db_session)

    await db_session.execute(
        update(AuthSession).where(AuthSession.id == session_id).values(revoked_at=func.now())
    )

    revoked = await db_session.scalar(
        select(AuthSession.revoked_at).where(AuthSession.id == session_id)
    )
    assert revoked is not None


@pytest.mark.parametrize(
    "change",
    [
        {"revoked_at": None},  # un-revoking
        {"revoked_at": text("now() + interval '1 second'")},  # revoking again
    ],
    ids=["un-revoke", "revoke-twice"],
)
async def test_a_revoked_session_stays_revoked(
    db_session: AsyncSession, change: dict[str, Any]
) -> None:
    session_id = await _session_row(db_session)
    await db_session.execute(
        update(AuthSession).where(AuthSession.id == session_id).values(revoked_at=func.now())
    )

    async with raises_violation("auth_sessions_revoke_only"):
        await db_session.execute(
            update(AuthSession).where(AuthSession.id == session_id).values(**change)
        )


@pytest.mark.parametrize("column", ["user_id", "token_hash", "expires_at", "created_at", "id"])
async def test_no_other_column_of_a_session_can_change(
    db_session: AsyncSession, column: str
) -> None:
    other_user = await create_user(db_session)
    session_id = await _session_row(db_session)
    new_value: Any = {
        "user_id": other_user.id,
        "token_hash": generate_token().token_hash,
        "expires_at": func.now() + timedelta(hours=1),
        "created_at": func.now() - timedelta(minutes=1),
        "id": uuid.uuid7(),
    }[column]

    async with raises_violation("auth_sessions_revoke_only"):
        await db_session.execute(
            update(AuthSession)
            .where(AuthSession.id == session_id)
            .values({column: new_value, "revoked_at": func.now()})  # even with a revocation
        )


async def test_a_session_can_be_deleted(db_session: AsyncSession) -> None:
    # Sessions are not financial evidence: a future cleanup deletes expired ones.
    session_id = await _session_row(db_session)

    await db_session.execute(delete(AuthSession).where(AuthSession.id == session_id))

    assert await db_session.get(AuthSession, session_id) is None


async def test_sessions_are_indexed_by_token_digest_and_by_user(db_session: AsyncSession) -> None:
    rows = await db_session.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'auth_sessions'")
    )
    indexes: dict[str, str] = {name: definition for name, definition in rows}

    assert set(indexes) == {
        "pk_auth_sessions",
        "uq_auth_sessions_token_hash",
        "ix_auth_sessions_user_id",
    }
    assert "UNIQUE" in indexes["uq_auth_sessions_token_hash"]
    assert "(token_hash)" in indexes["uq_auth_sessions_token_hash"]
    assert "(user_id)" in indexes["ix_auth_sessions_user_id"]


async def test_the_session_trigger_function_pins_its_search_path(db_session: AsyncSession) -> None:
    config = await db_session.scalar(
        text(
            "SELECT proconfig FROM pg_proc WHERE oid = "
            "'public.forbid_auth_session_changes()'::regprocedure"
        )
    )
    assert config == ["search_path=pg_catalog, pg_temp"]


# --- authentication audit actions ---------------------------------------------------------------


async def test_authentication_events_follow_the_audit_rules(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    records = [
        AuditRecord(AuditAction.AUTH_LOGIN, AuditOutcome.SUCCEEDED, actor_user_id=user.id),
        # A failed login for an unknown email: no actor, and nothing about the email.
        AuditRecord(AuditAction.AUTH_LOGIN, AuditOutcome.REJECTED, reason="invalid_credentials"),
        AuditRecord(AuditAction.AUTH_LOGOUT, AuditOutcome.SUCCEEDED, actor_user_id=user.id),
    ]
    before = await db_session.scalar(select(func.max(AuditEvent.id))) or 0

    for record in records:
        await insert_audit_event(db_session, record)

    events = (
        await db_session.scalars(
            select(AuditEvent).where(AuditEvent.id > before).order_by(AuditEvent.id)
        )
    ).all()
    assert [(e.action, e.outcome, e.actor_user_id, e.reason) for e in events] == [
        (AuditAction.AUTH_LOGIN, AuditOutcome.SUCCEEDED, user.id, None),
        (AuditAction.AUTH_LOGIN, AuditOutcome.REJECTED, None, "invalid_credentials"),
        (AuditAction.AUTH_LOGOUT, AuditOutcome.SUCCEEDED, user.id, None),
    ]
    assert all(event.transaction_id is None for event in events)


async def test_an_authentication_event_never_references_a_transaction(
    db_session: AsyncSession,
) -> None:
    user = await create_user(db_session)

    async with raises_violation("ck_audit_events_transaction_iff_posted"):
        await db_session.execute(
            insert(AuditEvent).values(
                action="auth.login",
                outcome="succeeded",
                actor_user_id=user.id,
                transaction_id=uuid.uuid7(),
            )
        )


async def test_a_successful_login_always_has_an_actor(db_session: AsyncSession) -> None:
    async with raises_violation("ck_audit_events_actor_for_success"):
        await db_session.execute(
            insert(AuditEvent).values(action="auth.login", outcome="succeeded")
        )


async def test_unknown_actions_are_still_rejected(db_session: AsyncSession) -> None:
    async with raises_violation("ck_audit_events_action"):
        await db_session.execute(
            insert(AuditEvent).values(action="auth.refresh", outcome="rejected", reason="nope")
        )
