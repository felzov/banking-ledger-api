import asyncio
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger_api.api.dependencies import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

# Bounded so a dead or unreachable database cannot hang the probe until the pool's timeout.
READINESS_TIMEOUT_SECONDS = 2.0


class HealthStatus(BaseModel):
    status: Literal["ok"]


class ReadinessStatus(BaseModel):
    status: Literal["ok", "unavailable"]


@router.get("/live")
async def liveness() -> HealthStatus:
    """Process is up and serving requests. Deliberately checks no dependencies."""
    return HealthStatus(status="ok")


@router.get(
    "/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessStatus}},
)
async def readiness(
    response: Response, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> ReadinessStatus:
    """Ready to serve traffic: PostgreSQL accepts a trivial query."""
    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except OSError, SQLAlchemyError, TimeoutError:
        # Details go to the log only: the response must not reveal infrastructure internals.
        logger.warning("readiness check failed: database unavailable", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessStatus(status="unavailable")
    return ReadinessStatus(status="ok")
