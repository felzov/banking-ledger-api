"""POST /users/{user_id}/accounts/{account_id}/{deposits,withdrawals,transfers} (ADR 0006).

Requests run on the test's rolled-back connection (api_client): the services still open and
commit their own transactions, which only release SAVEPOINTs here. Commit-level behaviour is
covered by the integration tests.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.api.routes import postings as postings_routes
from ledger_api.data.models import AuditEvent
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import CommitOutcomeUnknownError
from ledger_api.domain.posting import MAX_AMOUNT_MINOR
from ledger_api.services.posting import PostingResult
from tests.factories import get_settlement_account

POSTING_FIELDS = {
    "transaction_id",
    "kind",
    "account_id",
    "amount_minor",
    "balance_after_minor",
    "created_at",
}


def _key() -> str:
    return f"key-{uuid.uuid7().hex}"


async def _create_user(client: AsyncClient) -> str:
    response = await client.post("/users", json={"email": f"u-{uuid.uuid7().hex}@example.com"})
    assert response.status_code == 201, response.text
    user_id: str = response.json()["id"]
    return user_id


async def _open_account(client: AsyncClient, user_id: str, currency: str = "GBP") -> str:
    response = await client.post(f"/users/{user_id}/accounts", json={"currency": currency})
    assert response.status_code == 201, response.text
    account_id: str = response.json()["id"]
    return account_id


async def _customer(client: AsyncClient, currency: str = "GBP") -> tuple[str, str]:
    user_id = await _create_user(client)
    return user_id, await _open_account(client, user_id, currency)


async def _post(
    client: AsyncClient,
    user_id: str,
    account_id: str,
    operation: str,
    body: dict[str, Any],
    key: str | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Response:
    # Raw bytes: a header value that is not ASCII must reach the server as sent.
    if headers is None:
        headers = [(b"Idempotency-Key", (key or _key()).encode())]
    return await client.post(
        f"/users/{user_id}/accounts/{account_id}/{operation}", json=body, headers=headers
    )


async def _funded(client: AsyncClient, amount: int = 5_000) -> tuple[str, str]:
    user_id, account_id = await _customer(client)
    response = await _post(client, user_id, account_id, "deposits", {"amount_minor": amount})
    assert response.status_code == 201, response.text
    return user_id, account_id


async def _balance(client: AsyncClient, user_id: str, account_id: str) -> int:
    response = await client.get(f"/users/{user_id}/accounts/{account_id}")
    balance: int = response.json()["balance_minor"]
    return balance


# --- success and replay -------------------------------------------------------------------------


async def test_deposit_returns_201_with_the_posting(api_client: AsyncClient) -> None:
    user_id, account_id = await _customer(api_client)

    response = await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 2_500})

    assert response.status_code == 201
    body = response.json()
    assert set(body) == POSTING_FIELDS
    assert body["kind"] == "deposit"
    assert body["account_id"] == account_id
    assert (body["amount_minor"], body["balance_after_minor"]) == (2_500, 2_500)
    assert "idempotent-replayed" not in response.headers
    assert "location" not in response.headers  # no GET for transactions until Phase 7


async def test_withdrawal_returns_201_with_the_posting(api_client: AsyncClient) -> None:
    user_id, account_id = await _funded(api_client)

    response = await _post(api_client, user_id, account_id, "withdrawals", {"amount_minor": 1_200})

    assert response.status_code == 201
    body = response.json()
    assert (body["kind"], body["balance_after_minor"]) == ("withdrawal", 3_800)


async def test_transfer_returns_201_with_only_the_callers_balance(api_client: AsyncClient) -> None:
    user_id, account_id = await _funded(api_client)
    payee_user, payee_account = await _customer(api_client)

    response = await _post(
        api_client,
        user_id,
        account_id,
        "transfers",
        {"destination_account_id": payee_account, "amount_minor": 700},
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == POSTING_FIELDS | {"destination_account_id"}
    assert body["kind"] == "transfer"
    assert body["destination_account_id"] == payee_account
    assert body["balance_after_minor"] == 4_300  # the payer's; the payee's is never shown
    assert await _balance(api_client, payee_user, payee_account) == 700


@pytest.mark.parametrize("operation", ["deposits", "withdrawals", "transfers"])
async def test_a_retry_with_the_same_key_replays_the_original_response(
    api_client: AsyncClient, operation: str
) -> None:
    user_id, account_id = await _funded(api_client)
    _, payee_account = await _customer(api_client)
    body: dict[str, Any] = {"amount_minor": 300}
    if operation == "transfers":
        body["destination_account_id"] = payee_account
    key = _key()
    original = await _post(api_client, user_id, account_id, operation, body, key)

    replayed = await _post(api_client, user_id, account_id, operation, body, key)

    assert (original.status_code, replayed.status_code) == (201, 201)
    assert replayed.json() == original.json()
    assert replayed.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in original.headers


async def test_a_replayed_balance_is_historical(api_client: AsyncClient) -> None:
    user_id, account_id = await _customer(api_client)
    key = _key()
    original = await _post(
        api_client, user_id, account_id, "deposits", {"amount_minor": 1_000}, key
    )
    await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 500})
    await _post(api_client, user_id, account_id, "withdrawals", {"amount_minor": 200})

    replayed = await _post(
        api_client, user_id, account_id, "deposits", {"amount_minor": 1_000}, key
    )

    assert replayed.json()["balance_after_minor"] == 1_000 == original.json()["balance_after_minor"]
    assert await _balance(api_client, user_id, account_id) == 1_300


async def test_the_same_key_with_a_different_request_returns_422(api_client: AsyncClient) -> None:
    user_id, account_id = await _funded(api_client)
    key = _key()
    await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 100}, key)

    response = await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 101}, key)

    assert response.status_code == 422
    assert response.json() == {
        "code": "idempotency_key_mismatch",
        "detail": "This Idempotency-Key was already used for a different request.",
    }
    assert await _balance(api_client, user_id, account_id) == 5_100


async def test_a_rejected_request_can_be_retried_with_the_same_key(
    api_client: AsyncClient,
) -> None:
    user_id, account_id = await _customer(api_client)
    key = _key()

    rejected = await _post(
        api_client, user_id, account_id, "withdrawals", {"amount_minor": 500}, key
    )
    await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 1_000})
    retried = await _post(
        api_client, user_id, account_id, "withdrawals", {"amount_minor": 500}, key
    )

    assert (rejected.status_code, rejected.json()["code"]) == (422, "insufficient_funds")
    assert retried.status_code == 201
    assert "idempotent-replayed" not in retried.headers  # it ran: the rejection consumed nothing
    assert retried.json()["balance_after_minor"] == 500


# --- the Idempotency-Key header -----------------------------------------------------------------


async def test_a_missing_key_returns_428_and_is_not_audited(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id, account_id = await _funded(api_client)
    events = select(func.count()).select_from(AuditEvent)
    before = await db_session.scalar(events)

    response = await _post(
        api_client, user_id, account_id, "withdrawals", {"amount_minor": 1}, headers=[]
    )

    assert response.status_code == 428
    assert response.json() == {
        "code": "idempotency_key_required",
        "detail": "This request requires an Idempotency-Key header.",
    }
    assert await db_session.scalar(events) == before
    assert await _balance(api_client, user_id, account_id) == 5_000


@pytest.mark.parametrize(
    "headers",
    [
        [(b"Idempotency-Key", b"")],
        [(b"Idempotency-Key", b"with space")],
        [(b"Idempotency-Key", b"K" * 256)],
        [(b"Idempotency-Key", "caf\N{LATIN SMALL LETTER E WITH ACUTE}".encode())],
        [(b"Idempotency-Key", b"first"), (b"Idempotency-Key", b"second")],
    ],
    ids=["empty", "space", "too-long", "non-ascii", "repeated"],
)
async def test_a_malformed_key_returns_422_without_echoing_it(
    api_client: AsyncClient, headers: list[tuple[bytes, bytes]]
) -> None:
    user_id, account_id = await _funded(api_client)

    response = await _post(
        api_client, user_id, account_id, "withdrawals", {"amount_minor": 1}, headers=headers
    )

    assert response.status_code == 422
    assert response.json() == {
        "code": "invalid_idempotency_key",
        "detail": "The Idempotency-Key header must be one value of 1-255 visible ASCII characters.",
    }
    assert await _balance(api_client, user_id, account_id) == 5_000


async def test_the_key_header_is_case_insensitive(api_client: AsyncClient) -> None:
    user_id, account_id = await _customer(api_client)

    response = await _post(
        api_client,
        user_id,
        account_id,
        "deposits",
        {"amount_minor": 1},
        headers=[(b"idempotency-key", _key().encode())],
    )

    assert response.status_code == 201


async def test_keys_are_scoped_per_user(api_client: AsyncClient) -> None:
    key = _key()
    first_user, first_account = await _customer(api_client)
    second_user, second_account = await _customer(api_client)

    first = await _post(api_client, first_user, first_account, "deposits", {"amount_minor": 1}, key)
    second = await _post(
        api_client, second_user, second_account, "deposits", {"amount_minor": 1}, key
    )

    assert (first.status_code, second.status_code) == (201, 201)
    assert "idempotent-replayed" not in second.headers
    assert first.json()["transaction_id"] != second.json()["transaction_id"]


# --- request bodies -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"amount_minor": "100"},
        {"amount_minor": 100.0},
        {"amount_minor": 100.5},
        {"amount_minor": True},
        {"amount_minor": None},
        {},
        {"amount_minor": 100, "currency": "GBP"},
        {"amount_minor": 100, "balance_minor": 0},
    ],
)
async def test_malformed_bodies_return_validation_errors(
    api_client: AsyncClient, body: dict[str, Any]
) -> None:
    user_id, account_id = await _funded(api_client)

    response = await _post(api_client, user_id, account_id, "withdrawals", body)

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "validation_error"
    assert all(set(error) <= {"type", "loc", "msg"} for error in payload["detail"])
    assert await _balance(api_client, user_id, account_id) == 5_000


@pytest.mark.parametrize("amount", [0, -1, MAX_AMOUNT_MINOR + 1, 10**30])
async def test_out_of_range_amounts_are_invalid_amount(
    api_client: AsyncClient, amount: int
) -> None:
    user_id, account_id = await _funded(api_client)

    response = await _post(api_client, user_id, account_id, "deposits", {"amount_minor": amount})

    assert (response.status_code, response.json()["code"]) == (422, "invalid_amount")


async def test_transfer_without_a_valid_destination_is_a_validation_error(
    api_client: AsyncClient,
) -> None:
    user_id, account_id = await _funded(api_client)

    for body in ({"amount_minor": 1}, {"amount_minor": 1, "destination_account_id": "nope"}):
        response = await _post(api_client, user_id, account_id, "transfers", body)
        assert response.json()["code"] == "validation_error"


# --- business rules -----------------------------------------------------------------------------


async def test_business_rejections_map_to_their_codes(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id, account_id = await _funded(api_client, 100)
    other_user, other_account = await _customer(api_client)
    _, euro_account = await _customer(api_client, "EUR")
    settlement = await get_settlement_account(db_session, Currency.GBP)

    cases: list[tuple[str, str, str, dict[str, Any], int, str]] = [
        (user_id, account_id, "withdrawals", {"amount_minor": 101}, 422, "insufficient_funds"),
        (
            user_id,
            account_id,
            "transfers",
            {"destination_account_id": account_id, "amount_minor": 1},
            422,
            "same_account_transfer",
        ),
        (
            user_id,
            account_id,
            "transfers",
            {"destination_account_id": euro_account, "amount_minor": 1},
            422,
            "currency_mismatch",
        ),
        (
            user_id,
            account_id,
            "transfers",
            {"destination_account_id": str(uuid.uuid7()), "amount_minor": 1},
            404,
            "destination_account_not_found",
        ),
        (
            user_id,
            account_id,
            "transfers",
            {"destination_account_id": str(settlement.id), "amount_minor": 1},
            404,
            "destination_account_not_found",
        ),
        # Someone else's account, a settlement account, a missing account, an unknown user:
        # all the same 404 (ADR 0009).
        (user_id, other_account, "withdrawals", {"amount_minor": 1}, 404, "account_not_found"),
        (user_id, str(settlement.id), "deposits", {"amount_minor": 1}, 404, "account_not_found"),
        (user_id, str(uuid.uuid7()), "deposits", {"amount_minor": 1}, 404, "account_not_found"),
        (str(uuid.uuid7()), account_id, "deposits", {"amount_minor": 1}, 404, "account_not_found"),
    ]
    for owner, account, operation, body, status_code, code in cases:
        response = await _post(api_client, owner, account, operation, body)
        assert (response.status_code, response.json()["code"]) == (status_code, code), (
            operation,
            body,
        )

    assert await _balance(api_client, user_id, account_id) == 100
    assert await _balance(api_client, other_user, other_account) == 0


async def test_outcome_unknown_returns_503_telling_the_client_to_retry_with_the_same_key(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unknown(*args: Any, **kwargs: Any) -> PostingResult:
        raise CommitOutcomeUnknownError

    monkeypatch.setattr(postings_routes, "withdraw", unknown)
    user_id, account_id = await _funded(api_client)

    response = await _post(api_client, user_id, account_id, "withdrawals", {"amount_minor": 1})

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "outcome_unknown"
    assert "may or may not have been applied" in body["detail"]
    assert "same Idempotency-Key" in body["detail"]


async def test_openapi_documents_the_key_header_and_the_error_statuses(
    api_client: AsyncClient,
) -> None:
    schema = (await api_client.get("/openapi.json")).json()
    path = "/users/{user_id}/accounts/{account_id}/withdrawals"
    operation = schema["paths"][path]["post"]

    headers = [p["name"] for p in operation["parameters"] if p["in"] == "header"]
    assert headers == ["Idempotency-Key"]
    assert {"201", "404", "422", "428", "503"} <= set(operation["responses"])
