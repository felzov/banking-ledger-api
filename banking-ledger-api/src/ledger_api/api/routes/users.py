import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Request, Response, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ledger_api.api.dependencies import SessionDep
from ledger_api.api.errors import VALIDATION_ERROR_RESPONSE, ErrorResponse
from ledger_api.data.models import User
from ledger_api.domain.user import normalize_email
from ledger_api.services.users import get_user, register_user

# POST /users: public (it becomes registration in Phase 8).
router = APIRouter(prefix="/users", tags=["users"])

# Routes about one user. Mounted under the /users/{user_id} router built in main.py, which is
# where Phase 8 enforces "the caller is this user" once for every user-scoped route.
user_router = APIRouter(tags=["users"])


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # max_length bounds the raw input before validation; normalize_email applies the real rules
    # and stores only the canonical form.
    email: Annotated[str, Field(max_length=320), AfterValidator(normalize_email)]


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    created_at: datetime

    @classmethod
    def of(cls, user: User) -> UserResponse:
        return cls.model_validate(user)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_409_CONFLICT: {"model": ErrorResponse}, **VALIDATION_ERROR_RESPONSE},
)
async def create_user(
    body: CreateUserRequest, session: SessionDep, request: Request, response: Response
) -> UserResponse:
    user = await register_user(session, email=body.email)
    response.headers["Location"] = request.app.url_path_for("get_user", user_id=str(user.id))
    return UserResponse.of(user)


@user_router.get(
    "",
    name="get_user",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}, **VALIDATION_ERROR_RESPONSE},
)
async def read_user(user_id: uuid.UUID, session: SessionDep) -> UserResponse:
    return UserResponse.of(await get_user(session, user_id=user_id))
