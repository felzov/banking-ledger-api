"""JSON log lines and the request id on each of them (ADR 0017). No I/O beyond a memory stream."""

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from ledger_api.domain.audit import REQUEST_ID_PATTERN
from ledger_api.observability.correlation import (
    bind_request_id,
    current_request_id,
    new_request_id,
    unbind_request_id,
)
from ledger_api.observability.logs import configure_logging


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    configure_logging("INFO", stream=buffer)
    try:
        yield buffer
    finally:
        root = logging.getLogger()
        for handler in [h for h in root.handlers if h.get_name() == "ledger_api.json"]:
            root.removeHandler(handler)


def _entries(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_a_line_is_one_json_object_with_the_fixed_fields(stream: io.StringIO) -> None:
    logging.getLogger("ledger_api.services.posting").warning("failed after %d attempts", 3)

    (entry,) = _entries(stream)
    assert entry == {
        "time": entry["time"],
        "level": "WARNING",
        "logger": "ledger_api.services.posting",
        "message": "failed after 3 attempts",
        "request_id": None,  # outside a request
    }
    assert entry["time"].endswith("+00:00")


def test_the_current_request_id_is_on_every_line_whoever_logs(stream: io.StringIO) -> None:
    token = bind_request_id("request-1")
    try:
        logging.getLogger("ledger_api.data.engine").error("ours")
        logging.getLogger("uvicorn.error").error("the server's")
    finally:
        unbind_request_id(token)

    assert [entry["request_id"] for entry in _entries(stream)] == ["request-1", "request-1"]


def test_only_the_whitelisted_extras_are_written(stream: io.StringIO) -> None:
    # A stray extra= (a request body, a password) never reaches the output.
    logging.getLogger("ledger_api.access").info(
        "GET / 200", extra={"status": 200, "password": "hunter2-hunter2", "body": {"a": 1}}
    )

    (entry,) = _entries(stream)
    assert entry["status"] == 200
    assert "password" not in entry
    assert "body" not in entry
    assert "hunter2" not in stream.getvalue()


def test_an_exception_is_written_with_its_type_and_traceback(stream: io.StringIO) -> None:
    try:
        raise LookupError("nothing here")
    except LookupError:
        logging.getLogger("ledger_api").exception("failed")

    (entry,) = _entries(stream)
    assert entry["exc_type"] == "LookupError"
    assert "Traceback" in entry["traceback"]


def test_configuring_twice_writes_each_line_once(stream: io.StringIO) -> None:
    configure_logging("INFO", stream=stream)

    logging.getLogger("ledger_api").warning("once")

    assert len(_entries(stream)) == 1


def test_the_servers_own_access_log_is_silenced(stream: io.StringIO) -> None:
    # The request middleware writes the access line, with the request id (api.middleware).
    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET / HTTP/1.1" 200')

    assert _entries(stream) == []


def test_sqlalchemy_statements_and_their_parameters_stay_out(stream: io.StringIO) -> None:
    # At INFO the engine logs statements with their parameters: emails, hashes, token digests.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)
    configure_logging("DEBUG", stream=stream)

    logging.getLogger("sqlalchemy.engine.Engine").info("INSERT ... %s", "alice@example.com")

    assert _entries(stream) == []


def test_the_level_is_applied(stream: io.StringIO) -> None:
    configure_logging("WARNING", stream=stream)

    logging.getLogger("ledger_api").info("not written")
    logging.getLogger("ledger_api").warning("written")

    assert [entry["message"] for entry in _entries(stream)] == ["written"]


def test_request_ids_are_unique_and_fit_the_audit_column() -> None:
    first, second = new_request_id(), new_request_id()

    assert first != second
    assert REQUEST_ID_PATTERN.fullmatch(first)


async def test_the_request_id_follows_the_request_into_worker_threads() -> None:
    # Argon2 runs in asyncio.to_thread (ADR 0013): its log lines belong to the same request.
    token = bind_request_id("request-2")
    try:
        assert await asyncio.to_thread(current_request_id) == "request-2"
    finally:
        unbind_request_id(token)
    assert current_request_id() is None
