"""Money movement over HTTP: deposits, withdrawals and transfers (ADR 0006, ADR 0010).

Mounted under /users/{user_id} (see main.py): the posting's own account is always reached
through its owner. Every request needs an Idempotency-Key header. A retry with the same key and
the same request returns the original result (201, `Idempotent-Replayed: true`), rebuilt from
the ledger; the same key with a different request is refused (422 idempotency_key_mismatch).

Every 201, replays included, has a Location header: the transaction as it appears on the
caller's account (GET .../accounts/{account_id}/transactions/{transaction_id}).
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, StrictInt

from ledger_api.api.dependencies import IdempotencyKeyDep, SessionDep
from ledger_api.api.errors import VALIDATION_ERROR_RESPONSE, ErrorResponse
from ledger_api.domain.transaction import TransactionKind
from ledger_api.services.posting import PostingResult, deposit, transfer, withdraw

router = APIRouter(prefix="/accounts/{account_id}", tags=["postings"])

REPLAYED_HEADER = "Idempotent-Replayed"

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    **VALIDATION_ERROR_RESPONSE,
    status.HTTP_428_PRECONDITION_REQUIRED: {"model": ErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
}
CREATED_OR_REPLAYED: dict[str, Any] = {
    "description": "Posted, or replayed: then with the header `Idempotent-Replayed: true`.",
    "headers": {
        "Location": {
            "description": "The transaction as it appears on the caller's account.",
            "schema": {"type": "string"},
        },
        REPLAYED_HEADER: {
            "description": "`true` when an earlier request with the same key had posted this.",
            "schema": {"type": "string", "enum": ["true"]},
        },
    },
}


class AmountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # StrictInt: "100", 100.0 and true are rejected, never coerced. Whether the amount is
    # positive and within the per-transaction limit is a business rule, decided (and audited)
    # by the service as invalid_amount.
    amount_minor: StrictInt


class TransferRequest(AmountRequest):
    destination_account_id: uuid.UUID


class PostingResponse(BaseModel):
    transaction_id: uuid.UUID
    kind: TransactionKind
    # The caller's account, from the URL.
    account_id: uuid.UUID
    amount_minor: int
    # The caller's balance right after this posting; on a replay, after the ORIGINAL posting.
    balance_after_minor: int
    created_at: datetime


class TransferResponse(PostingResponse):
    destination_account_id: uuid.UUID


def _fields(result: PostingResult, account_id: uuid.UUID, amount_minor: int) -> dict[str, Any]:
    return {
        "transaction_id": result.transaction_id,
        "kind": result.kind,
        "account_id": account_id,
        "amount_minor": amount_minor,
        "balance_after_minor": result.balances[account_id],
        "created_at": result.created_at,
    }


def _created(
    request: Request,
    response: Response,
    result: PostingResult,
    *,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Location and the replay marker of a 201.

    Location names the transaction as it appears on the caller's account. Every part is
    already validated: user_id and account_id are the request's parsed path parameters (the
    account the service checked ownership of and posted on), and the transaction id comes from
    the service, the original one on a replay. It is never built from raw client text.
    """
    response.headers["Location"] = request.app.url_path_for(
        "get_account_transaction",
        user_id=str(user_id),
        account_id=str(account_id),
        transaction_id=str(result.transaction_id),
    )
    if result.replayed:
        response.headers[REPLAYED_HEADER] = "true"


@router.post(
    "/deposits",
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_201_CREATED: CREATED_OR_REPLAYED, **ERROR_RESPONSES},
)
async def create_deposit(
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    body: AmountRequest,
    idempotency_key: IdempotencyKeyDep,
    session: SessionDep,
    request: Request,
    response: Response,
) -> PostingResponse:
    result = await deposit(
        session,
        owner_id=user_id,
        account_id=account_id,
        amount_minor=body.amount_minor,
        idempotency_key=idempotency_key,
    )
    _created(request, response, result, user_id=user_id, account_id=account_id)
    return PostingResponse(**_fields(result, account_id, body.amount_minor))


@router.post(
    "/withdrawals",
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_201_CREATED: CREATED_OR_REPLAYED, **ERROR_RESPONSES},
)
async def create_withdrawal(
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    body: AmountRequest,
    idempotency_key: IdempotencyKeyDep,
    session: SessionDep,
    request: Request,
    response: Response,
) -> PostingResponse:
    result = await withdraw(
        session,
        owner_id=user_id,
        account_id=account_id,
        amount_minor=body.amount_minor,
        idempotency_key=idempotency_key,
    )
    _created(request, response, result, user_id=user_id, account_id=account_id)
    return PostingResponse(**_fields(result, account_id, body.amount_minor))


@router.post(
    "/transfers",
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_201_CREATED: CREATED_OR_REPLAYED, **ERROR_RESPONSES},
)
async def create_transfer(
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    body: TransferRequest,
    idempotency_key: IdempotencyKeyDep,
    session: SessionDep,
    request: Request,
    response: Response,
) -> TransferResponse:
    result = await transfer(
        session,
        owner_id=user_id,
        source_account_id=account_id,
        destination_account_id=body.destination_account_id,
        amount_minor=body.amount_minor,
        idempotency_key=idempotency_key,
    )
    _created(request, response, result, user_id=user_id, account_id=account_id)
    return TransferResponse(
        **_fields(result, account_id, body.amount_minor),
        destination_account_id=body.destination_account_id,
    )
