"""Login and logout (ADR 0013): POST /sessions and DELETE /sessions/current.

Login trades an email and password for an opaque bearer token, shown exactly once. Logout
revokes the token it is called with. Neither the password nor the token is ever echoed or
logged; failures answer {code, detail} with a WWW-Authenticate challenge.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr

from ledger_api.api.dependencies import (
    BearerTokenDep,
    PasswordHasherDep,
    RateLimiterDep,
    SessionDep,
    SessionLifetimeDep,
    limit_login_attempts,
)
from ledger_api.api.errors import (
    TOO_MANY_REQUESTS_RESPONSE,
    VALIDATION_ERROR_RESPONSE,
    ErrorResponse,
)
from ledger_api.api.routes.users import MAX_RAW_PASSWORD_LENGTH
from ledger_api.domain.user import normalize_email
from ledger_api.services.auth import login, logout

router = APIRouter(prefix="/sessions", tags=["sessions"])

UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "With a `WWW-Authenticate: Bearer` challenge.",
    }
}
# A token response must not be cached anywhere (RFC 6749, section 5.1).
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[str, Field(max_length=320), AfterValidator(normalize_email)]
    # No policy check here: a stored password always met it, so anything else just fails to
    # match, with the same 401 as any wrong password.
    password: Annotated[SecretStr, Field(max_length=MAX_RAW_PASSWORD_LENGTH)]


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 (the OAuth token type, not a secret)
    # Absolute expiry (database time); there is no refresh: log in again.
    expires_at: datetime


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={**UNAUTHORIZED, **VALIDATION_ERROR_RESPONSE, **TOO_MANY_REQUESTS_RESPONSE},
    dependencies=[Depends(limit_login_attempts)],
)
async def create_session(
    body: LoginRequest,
    session: SessionDep,
    hasher: PasswordHasherDep,
    lifetime: SessionLifetimeDep,
    limiter: RateLimiterDep,
    response: Response,
) -> LoginResponse:
    """Log in. An unknown email and a wrong password are the same 401 invalid_credentials.
    Rate limited per client address and per email's failed logins (429, ADR 0016)."""
    result = await login(
        session,
        hasher,
        email=body.email,
        password=body.password.get_secret_value(),
        lifetime=lifetime,
        limiter=limiter,
    )
    response.headers.update(NO_STORE)
    return LoginResponse(access_token=result.token, expires_at=result.expires_at)


@router.delete(
    "/current",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=UNAUTHORIZED,
)
async def delete_current_session(token: BearerTokenDep, session: SessionDep) -> None:
    """Log out: revoke the bearer token of this request. A missing header is 401
    authentication_required; an unknown, expired or already revoked token is 401
    invalid_token."""
    await logout(session, token=token)
