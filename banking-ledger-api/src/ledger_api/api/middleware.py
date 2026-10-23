"""Request correlation and the access log (ADR 0017).

The outermost application middleware: every request gets a server-generated id, which every log
line written while serving it carries, every audit event it writes records, and the response
returns as `X-Request-ID`. One access line per request replaces uvicorn's (log.configure_logging
silences that one): method, path without the query string, status, duration.

An exception that escapes the application leaves the id bound. Starlette's ServerErrorMiddleware
(outside this one) answers the 500 afterwards, through api.errors' handler, which adds the
header from it, and uvicorn then logs the traceback with it. Both run in this request's task,
after this middleware returned; uvicorn gives every request its own task, so the id ends with it.
On every other outcome it is unbound here.
"""

import logging
import time

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ledger_api.api.errors import REQUEST_ID_HEADER
from ledger_api.observability.correlation import (
    bind_request_id,
    new_request_id,
    unbind_request_id,
)

logger = logging.getLogger("ledger_api.access")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()
        token = bind_request_id(request_id)
        started = time.perf_counter()
        status_code = 500  # unless a response starts

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except BaseException:
            self._log(scope, status_code, started)
            raise  # the id stays bound: see the module docstring
        self._log(scope, status_code, started)
        unbind_request_id(token)

    @staticmethod
    def _log(scope: Scope, status_code: int, started: float) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        method, path = scope["method"], scope["path"]
        logger.info(
            "%s %s %d",
            method,
            path,
            status_code,
            extra={
                "method": method,
                "path": path,
                "status": status_code,
                "duration_ms": duration_ms,
            },
        )
