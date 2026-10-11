import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Request, Response, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr

from ledger_api.api.dependencies import PasswordHasherDep, SessionDep
from ledger_api.api.errors import VALIDATION_ERROR_RESPONSE, ErrorResponse
from ledger_api.data.models import User
from ledger_api.domain.passwords import normalize_password
from ledger_api.domain.user import normalize_email
from ledger_api.services.users import get_user, register_user_with_password

# POST /users: public registration (ADR 0013).
router = APIRouter(prefix="/users", tags=["users"])

# Bounds the raw input before NFKC normalization (which can lengthen text); the policy itself
# (12-128 characters after normalization) is domain.passwords'.
MAX_RAW_PASSWORD_LENGTH = 1_024


def _check_password_policy(password: SecretStr) -> SecretStr:
    # InvalidPasswordError is a ValueError with a fixed message: a 422 that never echoes it.
    normalize_password(password.get_secret_value())
    return password


# Routes about one user. Mounted under the /users/{user_id} router built in main.py, which is
# where Phase 8 enforces "the caller is this user" once for every user-scoped route.
user_router = APIRouter(tags=["users"])


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # max_length bounds the raw input before validation; normalize_email applies the real rules
    # and stores only the canonical form.
    email: Annotated[str, Field(max_length=320), AfterValidator(normalize_email)]
    # SecretStr: never shown by repr() or a log of the request model.
    password: Annotated[
        SecretStr,
        Field(max_length=MAX_RAW_PASSWORD_LENGTH),
        AfterValidator(_check_password_policy),
    ]


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
    body: CreateUserRequest,
    session: SessionDep,
    hasher: PasswordHasherDep,
    request: Request,
    response: Response,
) -> UserResponse:
    """Register a user who can then log in (POST /sessions). The password is never returned."""
    user = await register_user_with_password(
        session, hasher, email=body.email, password=body.password.get_secret_value()
    )
    response.headers["Location"] = request.app.url_path_for("get_user", user_id=str(user.id))
    return UserResponse.of(user)


@user_router.get(
    "",
    name="get_user",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}, **VALIDATION_ERROR_RESPONSE},
)
async def read_user(user_id: uuid.UUID, session: SessionDep) -> UserResponse:
    return UserResponse.of(await get_user(session, user_id=user_id))
