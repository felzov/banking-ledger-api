from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, status

from ledger_api.api.dependencies import require_path_user
from ledger_api.api.errors import ErrorResponse, register_error_handlers
from ledger_api.api.routes import accounts, health, postings, sessions, users
from ledger_api.config import Settings
from ledger_api.data.engine import create_engine, create_sessionmaker
from ledger_api.domain.passwords import make_hasher


def create_app(settings: Settings | None = None) -> FastAPI:
    # Loading settings here makes a misconfigured process fail at startup, not on first request.
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # One engine (and connection pool) per process, created here rather than at import time
        # so tests and workers each get their own. Creating it opens no connection, so the app
        # starts (and /health/live answers) even while the database is down.
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Banking Ledger API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    # One Argon2id hasher per process, with the configured cost (ADR 0013).
    app.state.password_hasher = make_hasher(settings.argon2_parameters())
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(sessions.router)
    app.include_router(_user_scoped_router())
    return app


USER_SCOPE = "/users/{user_id}"

# Documented once for every user-scoped route.
AUTHENTICATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "authentication_required or invalid_token, with a WWW-Authenticate "
        "Bearer challenge.",
    },
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "forbidden: {user_id} is not the authenticated user.",
    },
}


def _user_scoped_router() -> APIRouter:
    """Every route that acts on one user's data lives under /users/{user_id}.

    One router-level dependency authenticates the bearer token and requires the caller to be
    {user_id} (ADR 0009, ADR 0013); every route included below inherits it, and it runs before
    the route's own dependencies and body validation. Built per app because include_router
    copies routes: a module-level router would accumulate duplicates across create_app() calls.
    """
    scoped = APIRouter(
        dependencies=[Depends(require_path_user)], responses=AUTHENTICATION_RESPONSES
    )
    scoped.include_router(users.user_router, prefix=USER_SCOPE)
    scoped.include_router(accounts.router, prefix=USER_SCOPE)
    scoped.include_router(postings.router, prefix=USER_SCOPE)
    return scoped
