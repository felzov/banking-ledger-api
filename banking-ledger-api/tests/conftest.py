from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from ledger_api.config import Settings
from ledger_api.main import create_app

# Valid asyncpg URL on a port where nothing listens: the app starts, the database is "down".
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://ledger:synthetic@127.0.0.1:1/ledger"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    settings = Settings(app_env="test", database_url=SecretStr(UNREACHABLE_DATABASE_URL))
    transport = ASGITransport(app=create_app(settings))
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
