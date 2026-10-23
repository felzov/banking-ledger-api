"""Rate-limit counters in a real Redis (ADR 0016): counting, windows, concurrency, failures.

Every key lives under the test's own namespace (tests/conftest.py rate_limit_namespace).
"""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from ledger_api.data.rate_limits import (
    RateLimitStoreUnavailableError,
    connect,
    forget,
    hit,
)


@pytest.fixture
async def redis(test_redis_url: str) -> AsyncIterator[Redis]:
    client = connect(test_redis_url)
    try:
        yield client
    finally:
        await client.aclose()


async def test_hits_are_counted_within_one_window(redis: Redis, rate_limit_namespace: str) -> None:
    key = f"{rate_limit_namespace}:count"

    counts = [await hit(redis, key, 60) for _ in range(3)]

    assert [c.hits for c in counts] == [1, 2, 3]
    assert all(0 < c.remaining_ms <= 60_000 for c in counts)


async def test_the_window_starts_at_the_first_hit_and_later_hits_do_not_extend_it(
    redis: Redis, rate_limit_namespace: str
) -> None:
    # EXPIRE ... NX: only the first hit sets the expiry. A client hammering away must not keep
    # its own window open forever.
    key = f"{rate_limit_namespace}:window"
    first = await hit(redis, key, 60)
    await asyncio.sleep(0.2)

    later = await hit(redis, key, 60)

    assert later.remaining_ms <= first.remaining_ms - 150


async def test_a_new_window_starts_from_zero(redis: Redis, rate_limit_namespace: str) -> None:
    key = f"{rate_limit_namespace}:expiry"
    await hit(redis, key, 1)
    await hit(redis, key, 1)

    await asyncio.sleep(1.1)

    assert (await hit(redis, key, 1)).hits == 1


async def test_concurrent_hits_are_counted_exactly(redis: Redis, rate_limit_namespace: str) -> None:
    # INCR is atomic: 50 parallel requests get 50 distinct counts, none lost or duplicated.
    key = f"{rate_limit_namespace}:concurrent"

    counts = await asyncio.gather(*(hit(redis, key, 60) for _ in range(50)))

    assert sorted(c.hits for c in counts) == list(range(1, 51))


async def test_forgetting_resets_the_count(redis: Redis, rate_limit_namespace: str) -> None:
    key = f"{rate_limit_namespace}:forget"
    await hit(redis, key, 900)

    await forget(redis, key)

    assert await redis.exists(key) == 0
    assert (await hit(redis, key, 900)).hits == 1


async def test_an_error_redis_answers_is_unavailability(
    redis: Redis, rate_limit_namespace: str
) -> None:
    # A key of another type: INCR fails on the server. Still the store's failure, not a bug the
    # request should surface as a 500.
    key = f"{rate_limit_namespace}:wrongtype"
    await redis.rpush(key, "x")

    with pytest.raises(RateLimitStoreUnavailableError):
        await hit(redis, key, 60)


async def test_an_unreachable_redis_fails_fast_and_says_nothing_about_where() -> None:
    client = connect("redis://:synthetic-redis-password@127.0.0.1:1/0")
    started = time.monotonic()
    try:
        with pytest.raises(RateLimitStoreUnavailableError) as excinfo:
            await hit(client, "any", 60)
    finally:
        await client.aclose()

    assert time.monotonic() - started < 2  # one short attempt, no retries with backoff
    assert str(excinfo.value) == "the rate-limit store is unavailable"
    assert "synthetic-redis-password" not in str(excinfo.value)
