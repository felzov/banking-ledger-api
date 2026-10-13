"""A TCP proxy in front of PostgreSQL that can withhold the server's replies (tests only).

Everything the client sends still reaches the server at once, so the server executes it; only
what the server answers is held back until released. That pins a client between "PostgreSQL
has committed" and "the client has heard": a state no in-process hook can reach.

Like a real network path, an end of stream is passed on (after any bytes held before it):
asyncpg's cancel request, sent on a connection of its own, waits for the server to close it.
"""

import asyncio
from types import TracebackType
from typing import Self

_CLOSE = None  # in the held buffer: the server closed the connection here


class ReplyHoldingProxy:
    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self._holding = False
        self._held: list[tuple[asyncio.StreamWriter, bytes | None]] = []
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for writer in self._writers:
            writer.transport.abort()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def hold_replies(self) -> None:
        """From now on, buffer what the server sends instead of delivering it."""
        self._holding = True

    def release_replies(self) -> None:
        """Deliver everything held, in order, and stop holding."""
        self._holding = False
        held, self._held = self._held, []
        for writer, data in held:
            if data is _CLOSE:
                writer.close()
            else:
                writer.write(data)

    async def _serve(
        self, client_reader: asyncio.StreamReader, client: asyncio.StreamWriter
    ) -> None:
        server_reader, server = await asyncio.open_connection(*self._target)
        self._writers += [client, server]
        await asyncio.gather(
            self._pipe(client_reader, server, from_server=False),
            self._pipe(server_reader, client, from_server=True),
        )

    async def _pipe(
        self, source: asyncio.StreamReader, sink: asyncio.StreamWriter, *, from_server: bool
    ) -> None:
        try:
            while data := await source.read(65536):
                if from_server and self._holding:
                    self._held.append((sink, data))
                else:
                    sink.write(data)
                    await sink.drain()
        except ConnectionError:
            pass  # a reset: treated as a close
        if from_server and self._holding:
            self._held.append((sink, _CLOSE))
        else:
            sink.close()
