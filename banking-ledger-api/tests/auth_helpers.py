"""Authentication for API tests (ADR 0013).

The api_client fixture authenticates a request as the owner of the /users/{user_id} path ONLY
when the test signed that user up (signup) or granted it a token (grant_token), and only if the
request carries no Authorization header of its own. Requests for any other user id go out
without a token, so the enforcement is never bypassed: tests about authentication and
authorization pass explicit headers or use an unauthenticated client.
"""

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from weakref import WeakKeyDictionary

from httpx import AsyncClient, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.auth import insert_session
from ledger_api.domain.tokens import generate_token

TEST_PASSWORD = "correct horse battery staple"  # noqa: S105 (synthetic test data)

# The tokens of the users each test client may act as, by user id.
OWNER_TOKENS: WeakKeyDictionary[AsyncClient, dict[str, str]] = WeakKeyDictionary()

USER_PATH = re.compile(r"^/users/([^/]+)")


@dataclass(frozen=True)
class Caller:
    user_id: str
    token: str
    email: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def authenticate_as_path_owner(tokens: dict[str, str]) -> Callable[[Request], Awaitable[None]]:
    """An httpx request hook adding the path owner's token, for owners the test registered."""

    async def hook(request: Request) -> None:
        if "authorization" in request.headers:
            return
        match = USER_PATH.match(request.url.path)
        if match is not None and match.group(1) in tokens:
            request.headers["Authorization"] = f"Bearer {tokens[match.group(1)]}"

    return hook


async def log_in(client: AsyncClient, email: str, user_id: str) -> Caller:
    """Log a registered user in through the API; the client may then act as that user."""
    session = await client.post("/sessions", json={"email": email, "password": TEST_PASSWORD})
    assert session.status_code == 201, session.text
    caller = Caller(user_id, session.json()["access_token"], email)
    if client in OWNER_TOKENS:
        OWNER_TOKENS[client][caller.user_id] = caller.token
    return caller


async def signup(client: AsyncClient, email: str | None = None) -> Caller:
    """Register a user through the API and log in; the client may then act as that user."""
    email = email or f"u-{uuid.uuid7().hex}@example.com"
    registered = await client.post("/users", json={"email": email, "password": TEST_PASSWORD})
    assert registered.status_code == 201, registered.text
    return await log_in(client, registered.json()["email"], registered.json()["id"])


async def grant_token(client: AsyncClient, session: AsyncSession, user_id: uuid.UUID) -> str:
    """A live session for a user created without a password (factories), written directly on
    the test's rolled-back connection; the client may then act as that user."""
    issued = generate_token()
    await insert_session(
        session, user_id=user_id, token_hash=issued.token_hash, lifetime=timedelta(minutes=30)
    )
    await session.flush()
    if client in OWNER_TOKENS:
        OWNER_TOKENS[client][str(user_id)] = issued.token
    return issued.token
