"""Login and logout (ADR 0013).

Login:
  1. a short transaction finds the user and their password hash by email;
  2. outside any transaction, in a worker thread, Argon2id verifies the password: against the
     stored hash, or against a dummy hash when the email is unknown (or has no password), so
     that both failures cost the same and answer the same 401 invalid_credentials;
  3. a second transaction inserts the session (expiry from database time), rehashes the
     password if the hash parameters changed, and writes the succeeded audit event, together.
The slow hash therefore never runs on the event loop and never holds a database connection.

Logout revokes the caller's live session and audits it, in one transaction. A repeated logout
(or any unknown, expired or malformed token) is 401 invalid_token and is not audited, like any
unauthenticated request (ADR 0007).

Authentication (every protected request) looks the token's digest up in its own short
transaction and returns a plain Principal: the session is back outside any transaction, with
its connection in the pool, before the route's services open theirs (ADR 0001).

Never logged or audited: the password, the token, the token's hash, the email address.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.auth import (
    find_live_session,
    find_login_candidate,
    insert_session,
    replace_password_hash,
    revoke_live_session,
)
from ledger_api.domain.audit import AuditAction
from ledger_api.domain.errors import InvalidCredentialsError, InvalidTokenError
from ledger_api.domain.passwords import (
    Argon2Parameters,
    dummy_password_hash,
    hash_password,
    needs_rehash,
    verify_password,
)
from ledger_api.domain.principal import Principal
from ledger_api.domain.tokens import generate_token, hash_token, is_well_formed_token
from ledger_api.services.audit import AuditContext, audited, record_in_transaction


@dataclass(frozen=True, slots=True)
class LoginResult:
    # The bearer token, returned to the client exactly once. Never in repr().
    token: str = field(repr=False)
    session_id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime


def _parameters_of(hasher: PasswordHasher) -> Argon2Parameters:
    return Argon2Parameters(
        time_cost=hasher.time_cost,
        memory_cost=hasher.memory_cost,
        parallelism=hasher.parallelism,
        hash_len=hasher.hash_len,
        salt_len=hasher.salt_len,
    )


def _verify_against_dummy(hasher: PasswordHasher, password: str) -> None:
    """Pay for one Argon2id verification that cannot succeed (worker thread): an unknown email
    then costs what a wrong password costs."""
    verify_password(hasher, dummy_password_hash(_parameters_of(hasher)), password)


async def login(
    session: AsyncSession,
    hasher: PasswordHasher,
    *,
    email: str,
    password: str,
    lifetime: timedelta,
) -> LoginResult:
    """Open a session for the user registered with `email` (canonical form), or raise
    InvalidCredentialsError. Audited as auth.login: succeeded, or rejected with
    reason invalid_credentials (the actor is the email's user when there is one)."""
    context = AuditContext(AuditAction.AUTH_LOGIN, actor_user_id=None)

    async def run() -> LoginResult:
        async with session.begin():
            candidate = await find_login_candidate(session, email=email)
        # The transaction has ended and its connection is back in the pool.
        if candidate is None or candidate.password_hash is None:
            if candidate is not None:
                context.actor_user_id = candidate.user_id  # a user without a password
            await asyncio.to_thread(_verify_against_dummy, hasher, password)
            raise InvalidCredentialsError
        context.actor_user_id = candidate.user_id
        stored = candidate.password_hash
        if not await asyncio.to_thread(verify_password, hasher, stored, password):
            raise InvalidCredentialsError

        rehashed = None
        if needs_rehash(hasher, stored):
            rehashed = await asyncio.to_thread(hash_password, hasher, password)
        issued = generate_token()
        async with session.begin():
            stored_session = await insert_session(
                session, user_id=candidate.user_id, token_hash=issued.token_hash, lifetime=lifetime
            )
            if rehashed is not None:
                await replace_password_hash(
                    session, user_id=candidate.user_id, expected=stored, replacement=rehashed
                )
            await record_in_transaction(
                session, context.succeeded(created={"session_id": stored_session.session_id})
            )
        return LoginResult(
            token=issued.token,
            session_id=stored_session.session_id,
            user_id=stored_session.user_id,
            expires_at=stored_session.expires_at,
        )

    return await audited(session, context, run)


async def logout(session: AsyncSession, *, token: str) -> None:
    """Revoke the live session of `token`, or raise InvalidTokenError (unknown, expired,
    already revoked or malformed: one answer). Audited as auth.logout when it succeeds."""
    if not is_well_formed_token(token):
        raise InvalidTokenError
    async with session.begin():
        revoked = await revoke_live_session(session, token_hash=hash_token(token))
        if revoked is None:
            raise InvalidTokenError  # rolls back: nothing was written
        context = AuditContext(AuditAction.AUTH_LOGOUT, actor_user_id=revoked.user_id)
        await record_in_transaction(
            session, context.succeeded(created={"session_id": revoked.session_id})
        )


async def authenticate(session: AsyncSession, *, token: str) -> Principal:
    """The caller behind a bearer token, or InvalidTokenError (malformed, unknown, revoked or
    expired by the database's clock: one answer). Not audited: a request is authenticated, not
    an action taken. Runs in its own transaction and returns a plain value."""
    if not is_well_formed_token(token):
        raise InvalidTokenError
    async with session.begin():
        live = await find_live_session(session, token_hash=hash_token(token))
    if live is None:
        raise InvalidTokenError
    return Principal(user_id=live.user_id, session_id=live.session_id)
