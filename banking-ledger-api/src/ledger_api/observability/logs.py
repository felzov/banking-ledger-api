"""Structured logs: one JSON object per line, every line with the request's id (ADR 0017).

Standard library only. The formatter writes a fixed set of fields, plus the whitelisted extras
below; nothing else attached to a record reaches the output, so a stray `extra=` cannot leak a
request body. What goes into a message is the caller's responsibility, as before: never a
password, a token, a token's digest or an email address (ADR 0013).
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from ledger_api.observability.correlation import current_request_id

# Extras a log call may attach (logger.info(..., extra={...})): the access line's fields and a
# refused rate limit's name. Anything else is dropped.
EXTRA_FIELDS = ("method", "path", "status", "duration_ms", "rate_limit")

_HANDLER_NAME = "ledger_api.json"


class RequestIdFilter(logging.Filter):
    """Stamps the current request's id on every record the handler sees, including records of
    other libraries' loggers (uvicorn, SQLAlchemy) that propagate to the root."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        for name in EXTRA_FIELDS:
            if name in record.__dict__:
                entry[name] = record.__dict__[name]
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc_type"] = record.exc_info[0].__name__
            entry["traceback"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(level: str, *, stream: TextIO | None = None) -> None:
    """Send every log record, the application's and its libraries', through one JSON handler on
    the root logger. Idempotent: a second call replaces the handler, never adds another.

    uvicorn's loggers lose their own handlers and propagate to the root instead, so the server's
    lines are JSON too. Its access log is silenced: the request middleware writes one access
    line per request, with the request's id (api.middleware).
    """
    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(existing)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = True
    access.setLevel(logging.WARNING)
    # At INFO, SQLAlchemy's engine logs statements with their parameters: emails, password
    # hashes, token digests.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
