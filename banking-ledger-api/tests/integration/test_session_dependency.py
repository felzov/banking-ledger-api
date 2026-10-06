import uuid
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_api.api.dependencies import get_session
from ledger_api.data.models import User
from tests.conftest import serve


async def test_request_session_never_commits(app: FastAPI, engine: AsyncEngine) -> None:
    email = f"uncommitted-{uuid.uuid7().hex}@example.test"
    users_with_email = select(func.count()).select_from(User).where(User.email == email)

    async def write_without_commit(session: Annotated[AsyncSession, Depends(get_session)]) -> int:
        session.add(User(email=email))
        await session.flush()
        # Visible inside the request's own transaction: the write really happened.
        return await session.scalar(users_with_email) or 0

    app.add_api_route("/test-only/write-without-commit", write_without_commit, methods=["POST"])

    async with serve(app) as client:
        response = await client.post("/test-only/write-without-commit")

    assert response.status_code == 200
    assert response.json() == 1
    async with engine.connect() as connection:
        # ...and gone afterwards: closing the session rolled it back.
        assert await connection.scalar(users_with_email) == 0
