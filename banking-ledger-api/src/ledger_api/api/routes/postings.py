"""Money movement over HTTP: deposits, withdrawals and transfers (ADR 0006, ADR 0010).

Mounted under /users/{user_id} (see main.py): the posting's own account is always reached
through its owner. Every request needs an Idempotency-Key header. A retry with the same key and
the same request returns the original result (201, `Idempotent-Replayed: true`), rebuilt from
the ledger; the same key with a different request is refused (422 idempotency_key_mismatch).

There is no Location header: transactions have no GET endpoint yet (Phase 7).
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response, status
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
CREATED_OR_REPLAYED = {
    "description": "Posted, or replayed: then with the header `Idempotent-Replayed: true`."
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


def _mark_replay(response: Response, result: PostingResult) -> None:
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
    response: Response,
) -> PostingResponse:
    result = await deposit(
        session,
        owner_id=user_id,
        account_id=account_id,
        amount_minor=body.amount_minor,
        idempotency_key=idempotency_key,
    )
    _mark_replay(response, result)
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
    response: Response,
) -> PostingResponse:
    result = await withdraw(
        session,
        owner_id=user_id,
        account_id=account_id,
        amount_minor=body.amount_minor,
        idempotency_key=idempotency_key,
    )
    _mark_replay(response, result)
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
    _mark_replay(response, result)
    return TransferResponse(
        **_fields(result, account_id, body.amount_minor),
        destination_account_id=body.destination_account_id,
    )
