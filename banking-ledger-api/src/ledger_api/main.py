from fastapi import FastAPI

from ledger_api.api.routes import health
from ledger_api.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    # Loading settings here makes a misconfigured process fail at startup, not on first request.
    settings = settings or Settings()

    app = FastAPI(title="Banking Ledger API", version="0.1.0")
    app.state.settings = settings
    app.include_router(health.router)
    return app
