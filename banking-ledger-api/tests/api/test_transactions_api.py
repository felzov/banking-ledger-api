"""GET /users/{user_id}/accounts/{account_id}/transactions/{transaction_id}, and the Location
headers of the posting endpoints that point at it.

Requests run on the test's rolled-back connection (api_client): postings go through the real
posting endpoints and nothing outlives the test.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.domain.currency import Currency
from tests.conftest import TEST_PASSWORD
from tests.factories import get_settlement_account

ENTRY_FIELDS = {
    "entry_id",
    "transaction_id",
    "kind",
    "amount_minor",
    "balance_after_minor",
    "created_at",
}
TRANSACTION_NOT_FOUND = {"code": "transaction_not_found", "detail": "Transaction not found."}
ACCOUNT_NOT_FOUND = {"code": "account_not_found", "detail": "Account not found."}


async def _user(client: AsyncClient) -> str:
    response = await client.post(
        "/users", json={"email": f"u-{uuid.uuid7().hex}@example.com", "password": TEST_PASSWORD}
    )
    assert response.status_code == 201, response.text
    user_id: str = response.json()["id"]
    return user_id


async def _account(client: AsyncClient, user_id: str, currency: str = "GBP") -> str:
    response = await client.post(f"/users/{user_id}/accounts", json={"currency": currency})
    assert response.status_code == 201, response.text
    account_id: str = response.json()["id"]
    return account_id


async def _customer(client: AsyncClient) -> tuple[str, str]:
    user_id = await _user(client)
    return user_id, await _account(client, user_id)


async def _post(
    client: AsyncClient,
    user_id: str,
    account_id: str,
    operation: str,
    body: dict[str, Any],
    key: str | None = None,
) -> Response:
    response = await client.post(
        f"/users/{user_id}/accounts/{account_id}/{operation}",
        json=body,
        headers={"Idempotency-Key": key or f"key-{uuid.uuid7().hex}"},
    )
    assert response.status_code == 201, response.text
    return response


def _path(user_id: str, account_id: str, transaction_id: str) -> str:
    return f"/users/{user_id}/accounts/{account_id}/transactions/{transaction_id}"


async def _get(
    client: AsyncClient, user_id: str, account_id: str, transaction_id: str
) -> tuple[int, dict[str, Any]]:
    response = await client.get(_path(user_id, account_id, transaction_id))
    return response.status_code, response.json()


# --- reading a transaction ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "amount", "signed", "balance_after"),
    [
        ("deposits", 400, 400, 1_400),
        ("withdrawals", 300, -300, 700),
        ("transfers", 250, -250, 750),
    ],
)
async def test_a_posting_reads_back_from_the_owners_account(
    api_client: AsyncClient, operation: str, amount: int, signed: int, balance_after: int
) -> None:
    user_id, account_id = await _customer(api_client)
    _, payee_account = await _customer(api_client)
    await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 1_000})
    body: dict[str, Any] = {"amount_minor": amount}
    if operation == "transfers":
        body["destination_account_id"] = payee_account
    posted = (await _post(api_client, user_id, account_id, operation, body)).json()

    status_code, entry = await _get(api_client, user_id, account_id, posted["transaction_id"])

    assert status_code == 200
    assert set(entry) == ENTRY_FIELDS  # the statement-entry shape, no counterparty
    assert entry["transaction_id"] == posted["transaction_id"]
    assert entry["kind"] == posted["kind"]
    assert entry["amount_minor"] == signed
    assert entry["balance_after_minor"] == balance_after == posted["balance_after_minor"]
    assert entry["created_at"] == posted["created_at"]


async def test_a_transfer_read_from_the_payees_account_shows_only_the_payees_side(
    api_client: AsyncClient,
) -> None:
    payer_user, payer_account = await _customer(api_client)
    payee_user, payee_account = await _customer(api_client)
    await _post(api_client, payer_user, payer_account, "deposits", {"amount_minor": 1_000})
    await _post(api_client, payee_user, payee_account, "deposits", {"amount_minor": 30})
    sent = (
        await _post(
            api_client,
            payer_user,
            payer_account,
            "transfers",
            {"destination_account_id": payee_account, "amount_minor": 400},
        )
    ).json()

    status_code, entry = await _get(api_client, payee_user, payee_account, sent["transaction_id"])
    _, payer_entry = await _get(api_client, payer_user, payer_account, sent["transaction_id"])

    assert status_code == 200
    assert (entry["kind"], entry["amount_minor"], entry["balance_after_minor"]) == (
        "transfer",
        400,
        430,  # the payee's balance, never the payer's
    )
    assert entry["entry_id"] != payer_entry["entry_id"]  # each side is its own entry
    assert payer_account not in str(entry)
    assert payer_user not in str(entry)


async def test_the_balance_is_historical_after_later_postings(api_client: AsyncClient) -> None:
    user_id, account_id = await _customer(api_client)
    first = (
        await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 1_000})
    ).json()
    await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 500})
    await _post(api_client, user_id, account_id, "withdrawals", {"amount_minor": 200})

    _, entry = await _get(api_client, user_id, account_id, first["transaction_id"])
    account = (await api_client.get(f"/users/{user_id}/accounts/{account_id}")).json()

    assert entry["balance_after_minor"] == 1_000
    assert account["balance_minor"] == 1_300  # the current balance is not what is reported


async def test_a_transaction_reads_back_exactly_as_its_statement_entry(
    api_client: AsyncClient,
) -> None:
    user_id, account_id = await _customer(api_client)
    for amount in (100, 200, 300):
        await _post(api_client, user_id, account_id, "deposits", {"amount_minor": amount})
    statement = (await api_client.get(f"/users/{user_id}/accounts/{account_id}/statement")).json()

    for line in statement["entries"]:
        _, entry = await _get(api_client, user_id, account_id, line["transaction_id"])
        assert entry == line


# --- not found ----------------------------------------------------------------------------------


async def test_unknown_unrelated_and_other_account_transactions_are_the_same_404(
    api_client: AsyncClient,
) -> None:
    user_id, account_id = await _customer(api_client)
    euro_account = await _account(api_client, user_id, "EUR")  # the same user's other account
    mine = (await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 10})).json()
    other_user, other_account = await _customer(api_client)
    theirs = (
        await _post(api_client, other_user, other_account, "deposits", {"amount_minor": 10})
    ).json()
    third_user, third_account = await _customer(api_client)
    await _post(api_client, third_user, third_account, "deposits", {"amount_minor": 500})
    between_others = (
        await _post(
            api_client,
            third_user,
            third_account,
            "transfers",
            {"destination_account_id": other_account, "amount_minor": 5},
        )
    ).json()

    for account, transaction_id in [
        (account_id, str(uuid.uuid7())),  # does not exist
        (account_id, theirs["transaction_id"]),  # another user's deposit
        (account_id, between_others["transaction_id"]),  # a transfer between two other users
        (euro_account, mine["transaction_id"]),  # the user's own posting, on another account
    ]:
        status_code, body = await _get(api_client, user_id, account, transaction_id)
        assert (status_code, body) == (404, TRANSACTION_NOT_FOUND), (account, transaction_id)


async def test_the_account_is_checked_before_the_transaction(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    # Someone else's, a system or a missing account is account_not_found, even with a
    # transaction id that is valid on that account: nothing about it is revealed.
    user_id, _ = await _customer(api_client)
    other_user, other_account = await _customer(api_client)
    theirs = (
        await _post(api_client, other_user, other_account, "deposits", {"amount_minor": 10})
    ).json()
    settlement = await get_settlement_account(db_session, Currency.GBP)

    for owner, account in [
        (user_id, other_account),
        (user_id, str(settlement.id)),
        (user_id, str(uuid.uuid7())),
        (str(uuid.uuid7()), other_account),
    ]:
        status_code, body = await _get(api_client, owner, account, theirs["transaction_id"])
        assert (status_code, body) == (404, ACCOUNT_NOT_FOUND), (owner, account)


async def test_a_malformed_transaction_id_is_a_validation_error(api_client: AsyncClient) -> None:
    user_id, account_id = await _customer(api_client)

    response = await api_client.get(f"/users/{user_id}/accounts/{account_id}/transactions/nope")

    assert (response.status_code, response.json()["code"]) == (422, "validation_error")


# --- Location headers ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["deposits", "withdrawals", "transfers"])
async def test_every_posting_and_its_replay_carry_the_same_location(
    api_client: AsyncClient, operation: str
) -> None:
    user_id, account_id = await _customer(api_client)
    _, payee_account = await _customer(api_client)
    await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 1_000})
    body: dict[str, Any] = {"amount_minor": 100}
    if operation == "transfers":
        body["destination_account_id"] = payee_account
    key = f"key-{uuid.uuid7().hex}"

    original = await _post(api_client, user_id, account_id, operation, body, key)
    replayed = await _post(api_client, user_id, account_id, operation, body, key)

    expected = _path(user_id, account_id, original.json()["transaction_id"])
    assert original.headers["location"] == expected  # the caller's account, even for transfers
    assert replayed.headers["idempotent-replayed"] == "true"
    assert replayed.headers["location"] == expected  # the original transaction


@pytest.mark.parametrize("operation", ["deposits", "withdrawals", "transfers"])
async def test_following_location_returns_the_posted_transaction(
    api_client: AsyncClient, operation: str
) -> None:
    user_id, account_id = await _customer(api_client)
    _, payee_account = await _customer(api_client)
    await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 1_000})
    body: dict[str, Any] = {"amount_minor": 120}
    if operation == "transfers":
        body["destination_account_id"] = payee_account
    posted = await _post(api_client, user_id, account_id, operation, body)

    followed = await api_client.get(posted.headers["location"])

    assert followed.status_code == 200
    entry, created = followed.json(), posted.json()
    assert (entry["transaction_id"], entry["kind"], entry["balance_after_minor"]) == (
        created["transaction_id"],
        created["kind"],
        created["balance_after_minor"],
    )
    assert abs(entry["amount_minor"]) == created["amount_minor"]


async def test_a_replay_after_later_postings_still_points_at_the_original(
    api_client: AsyncClient,
) -> None:
    user_id, account_id = await _customer(api_client)
    key = f"key-{uuid.uuid7().hex}"
    original = await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 70}, key)
    await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 30})

    replayed = await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 70}, key)
    followed = (await api_client.get(replayed.headers["location"])).json()

    assert replayed.headers["location"] == original.headers["location"]
    assert followed["balance_after_minor"] == 70  # historical, as in the replayed body
    assert replayed.json()["balance_after_minor"] == 70


async def test_a_refused_posting_has_no_location(api_client: AsyncClient) -> None:
    user_id, account_id = await _customer(api_client)

    response = await api_client.post(
        f"/users/{user_id}/accounts/{account_id}/withdrawals",
        json={"amount_minor": 1},
        headers={"Idempotency-Key": f"key-{uuid.uuid7().hex}"},
    )

    assert (response.status_code, response.json()["code"]) == (422, "insufficient_funds")
    assert "location" not in response.headers


# --- OpenAPI ------------------------------------------------------------------------------------


async def test_openapi_documents_the_transaction_read(api_client: AsyncClient) -> None:
    schema = (await api_client.get("/openapi.json")).json()
    path = schema["paths"]["/users/{user_id}/accounts/{account_id}/transactions/{transaction_id}"]

    assert set(path) == {"get"}
    operation = path["get"]
    assert {p["name"] for p in operation["parameters"] if p["in"] == "path"} == {
        "user_id",
        "account_id",
        "transaction_id",
    }
    assert {"200", "404", "422"} <= set(operation["responses"])
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/StatementEntryResponse"}


@pytest.mark.parametrize("operation", ["deposits", "withdrawals", "transfers"])
async def test_openapi_documents_the_posting_location_header(
    api_client: AsyncClient, operation: str
) -> None:
    schema = (await api_client.get("/openapi.json")).json()
    created = schema["paths"][f"/users/{{user_id}}/accounts/{{account_id}}/{operation}"]["post"][
        "responses"
    ]["201"]

    assert {"Location", "Idempotent-Replayed"} <= set(created["headers"])
