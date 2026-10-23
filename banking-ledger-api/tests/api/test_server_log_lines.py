"""The real server's log lines (ADR 0017): uvicorn in-process on a free local port.

What only a real server shows: uvicorn logs the traceback of an exception that escapes the
application itself ("Exception in ASGI application"), after the request middleware has
returned. That line must carry the request's id too, or the one line that matters most could
not be tied to its request (api.middleware keeps the id bound for it).
"""

import asyncio
import io
import json
import logging
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from ledger_api.main import create_app
from ledger_api.observability.logs import configure_logging
from tests.conftest import UNREACHABLE_DATABASE_URL, settings_for


@asynccontextmanager
async def live_server(app: FastAPI) -> AsyncIterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    # log_config=None: uvicorn leaves logging alone; the app's lifespan configures it.
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started or serving.done():
                break
            await asyncio.sleep(0.01)
        assert server.started, "uvicorn did not start"
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await serving


@pytest.fixture
def log_lines() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    yield buffer
    root = logging.getLogger()
    for handler in [h for h in root.handlers if h.get_name() == "ledger_api.json"]:
        root.removeHandler(handler)


def _entries(buffer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


async def test_the_servers_traceback_line_carries_the_request_id(log_lines: io.StringIO) -> None:
    app = create_app(settings_for(UNREACHABLE_DATABASE_URL))

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    async with live_server(app) as url:
        configure_logging("INFO", stream=log_lines)  # after the lifespan sent it to stderr
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{url}/boom")

    request_id = response.headers["x-request-id"]
    entries = _entries(log_lines)
    (traceback,) = [e for e in entries if e.get("exc_type") == "RuntimeError"]
    assert traceback["logger"] == "uvicorn.error"
    assert traceback["request_id"] == request_id
    (access,) = [e for e in entries if e["logger"] == "ledger_api.access"]
    assert (access["status"], access["request_id"]) == (500, request_id)
    # Every line is JSON: nothing reached the server's own (plain-text) handlers.
    assert all(e["level"] for e in entries)
