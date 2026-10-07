from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


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
