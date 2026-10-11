import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict

from ledger_api.api.dependencies import SessionDep
from ledger_api.api.errors import (
    VALIDATION_ERROR_RESPONSE,
    VALIDATION_OR_RULE_ERROR_RESPONSE,
    ErrorResponse,
)
from ledger_api.data.models import Account
from ledger_api.domain.currency import Currency
from ledger_api.domain.transaction import TransactionKind
from ledger_api.services.accounts import (
    get_customer_account,
    list_customer_accounts,
    open_customer_account,
)
from ledger_api.services.statements import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    StatementEntry,
    StatementPage,
    get_account_transaction,
    get_statement,
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


class StatementEntryResponse(BaseModel):
    entry_id: uuid.UUID
    transaction_id: uuid.UUID
    kind: TransactionKind
    # Signed, for this account: negative debits it, positive credits it.
    amount_minor: int
    # The account's balance right after this entry: computed from the history, never stored.
    balance_after_minor: int
    # The posting transaction's start time. Entries are ordered by commit order, not by this.
    created_at: datetime
    # Deliberately no counterparty: a statement reveals only the owner's own account.

    @classmethod
    def of(cls, entry: StatementEntry) -> StatementEntryResponse:
        return cls(
            entry_id=entry.entry_id,
            transaction_id=entry.transaction_id,
            kind=entry.kind,
            amount_minor=entry.amount_minor,
            balance_after_minor=entry.balance_after_minor,
            created_at=entry.created_at,
        )


class StatementResponse(BaseModel):
    account_id: uuid.UUID
    # The current balance, from the same snapshot as the entries.
    balance_minor: int
    # Newest first.
    entries: list[StatementEntryResponse]
    # Pass as `cursor` for the next (older) page; null on the last page.
    next_cursor: uuid.UUID | None

    @classmethod
    def of(cls, page: StatementPage) -> StatementResponse:
        return cls(
            account_id=page.account_id,
            balance_minor=page.balance_minor,
            entries=[StatementEntryResponse.of(entry) for entry in page.entries],
            next_cursor=page.next_cursor,
        )


@router.get(
    "/{account_id}/statement",
    responses={
        **NOT_FOUND,
        **VALIDATION_OR_RULE_ERROR_RESPONSE,
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
    },
)
async def read_statement(
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    session: SessionDep,
    # Range checks stay in the service (invalid_statement_limit), as for posting amounts; the
    # bounds are documented here, not enforced twice.
    limit: Annotated[
        int,
        Query(description=f"Entries per page, 1-{MAX_LIMIT} (422 invalid_statement_limit)."),
    ] = DEFAULT_LIMIT,
    cursor: Annotated[
        uuid.UUID | None,
        Query(
            description=(
                "The previous page's next_cursor. An unknown id or another account's entry: "
                "422 invalid_statement_cursor."
            )
        ),
    ] = None,
) -> StatementResponse:
    """The account's history, newest first, with the balance after each entry (ADR 0012).

    Keyset pagination: follow next_cursor until it is null. Postings made between page
    requests appear only on a fresh first page; following the cursor never repeats or skips an
    entry. Someone else's, a system or a missing account is 404 account_not_found.
    """
    page = await get_statement(
        session, owner_id=user_id, account_id=account_id, limit=limit, cursor=cursor
    )
    return StatementResponse.of(page)


@router.get(
    "/{account_id}/transactions/{transaction_id}",
    name="get_account_transaction",
    responses={**NOT_FOUND, **VALIDATION_ERROR_RESPONSE},
)
async def read_account_transaction(
    user_id: uuid.UUID, account_id: uuid.UUID, transaction_id: uuid.UUID, session: SessionDep
) -> StatementEntryResponse:
    """One transaction as it appears on this account: the account's own entry, signed, with the
    balance right after it (historical, as on the statement). No counterparty.

    Someone else's, a system or a missing account is 404 account_not_found. A transaction that
    does not exist, or has no entry on this account, is the same 404 transaction_not_found.
    """
    entry = await get_account_transaction(
        session, owner_id=user_id, account_id=account_id, transaction_id=transaction_id
    )
    return StatementEntryResponse.of(entry)
