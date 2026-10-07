import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict

from ledger_api.api.dependencies import SessionDep
from ledger_api.api.errors import VALIDATION_ERROR_RESPONSE, ErrorResponse
from ledger_api.data.models import Account
from ledger_api.domain.currency import Currency
from ledger_api.services.accounts import (
    get_customer_account,
    list_customer_accounts,
    open_customer_account,
)

# Mounted under /users/{user_id} (see main.py): accounts are only reachable through their owner.
router = APIRouter(prefix="/accounts", tags=["accounts"])

NOT_FOUND: dict[int | str, dict[str, Any]] = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}}


class OpenAccountRequest(BaseModel):
    # Only the currency. The owner comes from the URL, the kind is always customer and the
    # balance always starts at 0: none of them is client-controlled, and extra fields
    # (kind, balance_minor, user_id, ...) are rejected rather than silently ignored.
    model_config = ConfigDict(extra="forbid")

    currency: Currency


class AccountResponse(BaseModel):
    id: uuid.UUID
    currency: Currency
    balance_minor: int
    created_at: datetime

    @classmethod
    def of(cls, account: Account) -> AccountResponse:
        return cls(
            id=account.id,
            currency=account.ledger.currency,
            balance_minor=account.balance_minor,
            created_at=account.created_at,
        )


class AccountListResponse(BaseModel):
    # A wrapper rather than a bare list, so pagination can be added without breaking clients.
    items: list[AccountResponse]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={
        **NOT_FOUND,
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
        **VALIDATION_ERROR_RESPONSE,
    },
)
async def open_account(
    user_id: uuid.UUID,
    body: OpenAccountRequest,
    session: SessionDep,
    request: Request,
    response: Response,
) -> AccountResponse:
    account = await open_customer_account(session, owner_id=user_id, currency=body.currency)
    response.headers["Location"] = request.app.url_path_for(
        "get_account", user_id=str(user_id), account_id=str(account.id)
    )
    return AccountResponse.of(account)


@router.get("", responses={**NOT_FOUND, **VALIDATION_ERROR_RESPONSE})
async def list_accounts(user_id: uuid.UUID, session: SessionDep) -> AccountListResponse:
    accounts = await list_customer_accounts(session, owner_id=user_id)
    return AccountListResponse(items=[AccountResponse.of(account) for account in accounts])


@router.get(
    "/{account_id}", name="get_account", responses={**NOT_FOUND, **VALIDATION_ERROR_RESPONSE}
)
async def read_account(
    user_id: uuid.UUID, account_id: uuid.UUID, session: SessionDep
) -> AccountResponse:
    account = await get_customer_account(session, owner_id=user_id, account_id=account_id)
    return AccountResponse.of(account)
