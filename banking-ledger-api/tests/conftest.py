from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from ledger_api.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
