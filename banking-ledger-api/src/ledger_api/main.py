from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from ledger_api.api.errors import register_error_handlers
from ledger_api.api.routes import health, users
from ledger_api.config import Settings
from ledger_api.data.engine import create_engine, create_sessionmaker


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
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(_user_scoped_router())
    return app


USER_SCOPE = "/users/{user_id}"


def _user_scoped_router() -> APIRouter:
    """Every route that acts on one user's data lives under /users/{user_id}.

    UNAUTHENTICATED until Phase 8, which adds a single router-level dependency to `scoped`
    requiring the caller to be {user_id}; every route included below inherits it (ADR 0009).
    Built per app because include_router copies routes: a module-level router would
    accumulate duplicates across create_app() calls.
    """
    scoped = APIRouter()
    scoped.include_router(users.user_router, prefix=USER_SCOPE)
    return scoped
