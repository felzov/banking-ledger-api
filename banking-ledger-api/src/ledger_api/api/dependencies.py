from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_api.api.errors import IdempotencyKeyRequiredError, InvalidIdempotencyKeyError
from ledger_api.domain.idempotency import is_valid_idempotency_key


def get_engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One AsyncSession per request; never shared between concurrent tasks (ADR 0001).

    This dependency never commits. Services own transaction boundaries
    (`async with session.begin():`); anything left uncommitted is rolled back when the session
    closes at the end of the request.
    """
    async with request.app.state.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


def get_idempotency_key(
    request: Request,
    # Declared for the OpenAPI schema; the value is read below, from every occurrence.
    idempotency_key: Annotated[
        str | None,
        Header(
            alias=IDEMPOTENCY_KEY_HEADER,
            description=(
                "Required. 1-255 visible ASCII characters, unique per intended operation of "
                "this user. A retry with the same key and request replays the original result."
            ),
        ),
    ] = None,
) -> str:
    """The request's Idempotency-Key (ADR 0006). Missing: 428. Malformed or repeated: 422.

    Checked before the body reaches any service, so neither case is audited. The value is
    never echoed back.
    """
    values = request.headers.getlist(IDEMPOTENCY_KEY_HEADER)
    if not values:
        raise IdempotencyKeyRequiredError
    # Two keys on one request are ambiguous: refuse rather than pick one.
    if len(values) != 1 or not is_valid_idempotency_key(values[0]):
        raise InvalidIdempotencyKeyError
    return values[0]


IdempotencyKeyDep = Annotated[str, Depends(get_idempotency_key)]
