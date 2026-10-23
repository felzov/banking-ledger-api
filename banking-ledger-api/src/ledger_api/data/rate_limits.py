"""Rate-limit counters in Redis (ADR 0008, ADR 0016). Nothing else is ever stored there.

A counter is a fixed window that starts with its first hit: INCR, and EXPIRE ... NX so only that
first hit sets the window, in one MULTI/EXEC. Redis's own clock ends the window; the application
clock never matters. Any failure to reach Redis, or an error it answers, is
RateLimitStoreUnavailableError: the caller decides whether that refuses the request.

The client fails fast: a short socket timeout and no retries. A limiter sits on the request path,
and a dead Redis must cost one short timeout, not several with backoff.
"""

from dataclasses import dataclass
from typing import cast

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

# Per connect and per command. A local Redis answers in well under a millisecond.
SOCKET_TIMEOUT_SECONDS = 0.25


class RateLimitStoreUnavailableError(ConnectionError):
    """Redis could not be reached, timed out, or answered with an error. The message names no
    host, port or password; the redis-py error is the __cause__."""


@dataclass(frozen=True, slots=True)
class Count:
    hits: int
    # Until the window ends, by Redis's clock.
    remaining_ms: int


def connect(url: str) -> Redis:
    """A client for `url`. Opens no connection until the first command."""
    return Redis.from_url(
        url,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), retries=0),
    )


def _remaining(ttl_ms: int, window_seconds: int) -> int:
    # -2: no key (already expired); -1: no expiry (cannot happen here). Either way: a full window.
    return ttl_ms if ttl_ms > 0 else window_seconds * 1000


async def hit(client: Redis, key: str, window_seconds: int) -> Count:
    """Count one hit and return the count so far in this window."""
    try:
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_seconds, nx=True)
            pipe.pttl(key)
            hits, _, ttl_ms = cast(list[int], await pipe.execute())
    except (RedisError, OSError) as error:
        raise RateLimitStoreUnavailableError("the rate-limit store is unavailable") from error
    return Count(hits=hits, remaining_ms=_remaining(ttl_ms, window_seconds))


async def forget(client: Redis, key: str) -> None:
    try:
        await client.delete(key)
    except (RedisError, OSError) as error:
        raise RateLimitStoreUnavailableError("the rate-limit store is unavailable") from error
