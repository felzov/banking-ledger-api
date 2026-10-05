from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class HealthStatus(BaseModel):
    status: Literal["ok"]


@router.get("/live")
async def liveness() -> HealthStatus:
    """Process is up and serving requests. Deliberately checks no dependencies."""
    return HealthStatus(status="ok")
