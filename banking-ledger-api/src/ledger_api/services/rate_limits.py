"""The API's rate limits (ADR 0016), counted in Redis (ADR 0008).

| Limit                | Counted per    | Counts                    | Redis unavailable          |
|----------------------|----------------|---------------------------|----------------------------|
| login_address        | client address | every POST /sessions      | refused: 503 (fail closed) |
| login_failures       | email address  | logins since last success | refused: 503 (fail closed) |
| registration_address | client address | every POST /users         | refused: 503 (fail closed) |
| postings_user        | user           | every posting             | allowed, WARNING (open)    |

Fail closed where the limit is the defence against guessing passwords or enumerating emails;
fail open where it only bounds an authenticated user's load, so a Redis outage never stops
money from moving. Over a limit: RateLimitedError (429, Retry-After). A refusal is logged with
the limit's name, never the address, email or user it counted, and is not audited: like any
request refused at the door, it changed nothing (ADR 0007).

login_failures is a throttle, not a lockout: once an email has had `maximum` login attempts since
its last successful login, within the window, every further login for it is refused, correct
password included, until the window ends; a successful login clears the count. Each attempt is
counted before its password is checked, so concurrent attempts cannot outrun the limit. Unknown
and registered emails are counted alike, so the limit reveals nothing about which emails exist.
The cost: anyone can lock an email out for one window by failing on purpose.
"""

import logging
import math
import uuid

from redis.asyncio import Redis

from ledger_api.data.rate_limits import RateLimitStoreUnavailableError, forget, hit
from ledger_api.domain.errors import RateLimitedError, TemporarilyUnavailableError
from ledger_api.domain.rate_limits import (
    Limit,
    RateLimits,
    address_subject,
    counter_key,
    email_subject,
    user_subject,
)

logger = logging.getLogger(__name__)


def _retry_after(remaining_ms: int) -> int:
    return max(1, math.ceil(remaining_ms / 1000))


class RateLimiter:
    def __init__(self, client: Redis, *, namespace: str, limits: RateLimits) -> None:
        self._client = client
        self._namespace = namespace
        self.limits = limits

    async def limit_login_address(self, host: str | None) -> None:
        await self._count(self.limits.login_address, address_subject(host), fail_open=False)

    async def limit_registration_address(self, host: str | None) -> None:
        await self._count(self.limits.registration_address, address_subject(host), fail_open=False)

    async def limit_postings(self, user_id: uuid.UUID) -> None:
        await self._count(self.limits.postings_user, user_subject(user_id), fail_open=True)

    async def reserve_login_attempt(self, email: str) -> None:
        """Before a login is checked at all: count it, and refuse it (computing no hash) if the
        email has had `maximum` attempts since its last successful login, in this window.

        Counted before the password is checked, in one atomic INCR, never checked first and
        counted after: concurrent attempts would all pass the check before any of them was
        counted (review fix B1). A success clears the count, so in effect it counts failures."""
        await self._count(self.limits.login_failures, email_subject(email), fail_open=False)

    async def clear_login_failures(self, email: str) -> None:
        """After a successful login. Best effort: a count left behind only expires later."""
        limit = self.limits.login_failures
        try:
            await forget(self._client, self._key(limit, email_subject(email)))
        except RateLimitStoreUnavailableError:
            logger.warning(
                "rate limit %s: could not clear a count",
                limit.name,
                extra={"rate_limit": limit.name},
            )

    async def _count(self, limit: Limit, subject: str, *, fail_open: bool) -> None:
        try:
            count = await hit(self._client, self._key(limit, subject), limit.window_seconds)
        except RateLimitStoreUnavailableError as error:
            if fail_open:
                logger.warning(
                    "rate limit %s not enforced: the rate-limit store is unavailable",
                    limit.name,
                    extra={"rate_limit": limit.name},
                )
                return
            raise self._unavailable(limit) from error
        if count.hits > limit.maximum:
            raise self._refused(limit, count.remaining_ms)

    def _key(self, limit: Limit, subject: str) -> str:
        return counter_key(self._namespace, limit, subject)

    @staticmethod
    def _refused(limit: Limit, remaining_ms: int) -> RateLimitedError:
        logger.warning("rate limit %s exceeded", limit.name, extra={"rate_limit": limit.name})
        return RateLimitedError(_retry_after(remaining_ms))

    @staticmethod
    def _unavailable(limit: Limit) -> TemporarilyUnavailableError:
        logger.error(
            "rate limit %s cannot be checked: the rate-limit store is unavailable; refusing",
            limit.name,
            extra={"rate_limit": limit.name},
        )
        return TemporarilyUnavailableError()
