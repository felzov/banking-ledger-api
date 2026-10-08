"""Maps errors to HTTP responses. Every error body has the shape {"code", "detail"}."""

from typing import Any, Literal

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ledger_api.domain.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    RuleViolationError,
    UnavailableError,
)

# Domain errors are mapped by category, so a new error type needs no change here.
STATUS_BY_CATEGORY: dict[type[DomainError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    RuleViolationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    UnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}

# Fields of a validation error that are safe to return. Pydantic's "input" echoes the
# submitted value (a password, from Phase 8 on) and "ctx" can hold the raw exception.
SAFE_VALIDATION_FIELDS = ("type", "loc", "msg")


class ErrorResponse(BaseModel):
    code: str
    detail: str


class ValidationErrorResponse(BaseModel):
    code: Literal["validation_error"]
    detail: list[dict[str, Any]]


def status_for(error: DomainError) -> int:
    for category, status_code in STATUS_BY_CATEGORY.items():
        if isinstance(error, category):
            return status_code
    # An unmapped category is a programming error: fail loudly (500) rather than guess.
    raise LookupError(f"no HTTP status mapped for {type(error).__name__}")


async def _domain_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):  # registered for DomainError only
        raise exc
    body = ErrorResponse(code=exc.code, detail=exc.message)
    return JSONResponse(status_code=status_for(exc), content=body.model_dump())


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # registered for it only
        raise exc
    errors = [
        {field: error[field] for field in SAFE_VALIDATION_FIELDS if field in error}
        for error in exc.errors()
    ]
    body = ValidationErrorResponse(code="validation_error", detail=errors)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=body.model_dump()
    )


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Starlette re-raises the exception after sending this response, so the server logs the
    # traceback; nothing about it reaches the client.
    body = ErrorResponse(code="internal_error", detail="Internal server error.")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body.model_dump()
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)


# Shared OpenAPI documentation for error responses.
VALIDATION_ERROR_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ValidationErrorResponse}
}
