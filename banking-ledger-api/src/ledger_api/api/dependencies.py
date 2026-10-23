import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from argon2 import PasswordHasher
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_api.api.errors import (
    AuthenticationRequiredError,
    ForbiddenError,
    IdempotencyKeyRequiredError,
    InvalidIdempotencyKeyError,
)
from ledger_api.config import Settings
from ledger_api.domain.idempotency import is_valid_idempotency_key
from ledger_api.domain.principal import Principal
from ledger_api.services.auth import authenticate
from ledger_api.services.rate_limits import RateLimiter


def get_engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One AsyncSession per request; never shared between concurrent tasks (ADR 0001).

    This dependency never commits. Every service, reads included, owns its transaction
    boundaries (`async with session.begin():`) and returns the session outside any transaction,
    so several service calls can share it. Anything left uncommitted is rolled back when the
    session closes at the end of the request.
    """
    async with request.app.state.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


def get_idempotency_key(
    request: Request,
    # Declared for the OpenAPI schema; the value is read below, from every occurrence.
    idempotency_key: Annotated[
        str | None,
        Header(
            alias=IDEMPOTENCY_KEY_HEADER,
            description=(
                "Required. 1-255 visible ASCII characters, unique per intended operation of "
                "this user. A retry with the same key and request replays the original result."
            ),
        ),
    ] = None,
) -> str:
    """The request's Idempotency-Key (ADR 0006). Missing: 428. Malformed or repeated: 422.

    Checked before the body reaches any service, so neither case is audited. The value is
    never echoed back.
    """
    values = request.headers.getlist(IDEMPOTENCY_KEY_HEADER)
    if not values:
        raise IdempotencyKeyRequiredError
    # Two keys on one request are ambiguous: refuse rather than pick one.
    if len(values) != 1 or not is_valid_idempotency_key(values[0]):
        raise InvalidIdempotencyKeyError
    return values[0]


IdempotencyKeyDep = Annotated[str, Depends(get_idempotency_key)]


def get_password_hasher(request: Request) -> PasswordHasher:
    """The process's Argon2id hasher, built once from the settings (create_app)."""
    hasher: PasswordHasher = request.app.state.password_hasher
    return hasher


def get_session_lifetime(request: Request) -> timedelta:
    settings: Settings = request.app.state.settings
    return timedelta(seconds=settings.auth_session_ttl_seconds)


PasswordHasherDep = Annotated[PasswordHasher, Depends(get_password_hasher)]
SessionLifetimeDep = Annotated[timedelta, Depends(get_session_lifetime)]

# Documents the Bearer scheme in OpenAPI. auto_error=False: a missing or non-Bearer header is
# answered by this API's own 401 authentication_required, in its {code, detail} format.
bearer_scheme = HTTPBearer(auto_error=False, description="An access token from POST /sessions.")


def get_bearer_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> str:
    """The token of `Authorization: Bearer <token>`. Missing, empty, repeated or another
    scheme: 401 authentication_required. Whether the token is valid is the service's call
    (401 invalid_token). The token is never logged."""
    if credentials is None or len(request.headers.getlist("Authorization")) != 1:
        raise AuthenticationRequiredError
    return credentials.credentials


BearerTokenDep = Annotated[str, Depends(get_bearer_token)]


async def get_principal(token: BearerTokenDep, session: SessionDep) -> Principal:
    """The authenticated caller (401 otherwise). The lookup runs in its own transaction on the
    request's session, which is back outside any transaction when this returns, so the route's
    services open their own as usual (ADR 0001). Only a plain Principal goes downstream."""
    return await authenticate(session, token=token)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_path_user(user_id: uuid.UUID, principal: PrincipalDep) -> Principal:
    """The authorization rule of every /users/{user_id}/... route (ADR 0009, ADR 0013): the
    caller must be {user_id}, else 403 forbidden, before any service runs. Mounted once, on the
    router every user-scoped route is included in."""
    if principal.user_id != user_id:
        raise ForbiddenError
    return principal


# --- rate limits (ADR 0016) -----------------------------------------------------------------------


def get_rate_limiter(request: Request) -> RateLimiter | None:
    """The process's limiter (main.py lifespan), or None when REDIS_URL is unset: no limits."""
    limiter: RateLimiter | None = request.app.state.rate_limiter
    return limiter


RateLimiterDep = Annotated[RateLimiter | None, Depends(get_rate_limiter)]


def _client_host(request: Request) -> str | None:
    # The peer uvicorn reports; behind a proxy, the proxy's own address unless uvicorn trusts it
    # (--forwarded-allow-ips) and takes the client from X-Forwarded-For (README, Security).
    return request.client.host if request.client is not None else None


async def limit_login_attempts(request: Request, limiter: RateLimiterDep) -> None:
    """POST /sessions, every attempt per client address. A dependency, so it runs before the
    body is validated: malformed attempts count too."""
    if limiter is not None:
        await limiter.limit_login_address(_client_host(request))


async def limit_registrations(request: Request, limiter: RateLimiterDep) -> None:
    """POST /users, every attempt per client address: bounds email enumeration (ADR 0008)."""
    if limiter is not None:
        await limiter.limit_registration_address(_client_host(request))


async def limit_postings(principal: PrincipalDep, limiter: RateLimiterDep) -> None:
    """Every posting per authenticated user, replays included. After authentication and the
    ownership check (the router's dependency runs first; the principal is cached per request)."""
    if limiter is not None:
        await limiter.limit_postings(principal.user_id)
