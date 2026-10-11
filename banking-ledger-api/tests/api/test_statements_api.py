"""GET /users/{user_id}/accounts/{account_id}/statement (ADR 0012).

Requests run on the test's rolled-back connection (api_client): postings go through the real
posting endpoints, and nothing outlives the test. The statement semantics themselves (one
snapshot, commit order, fail-closed) are covered by tests/integration/test_statements.py.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.api.routes import accounts as accounts_routes
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import StatementInconsistencyError
from ledger_api.services.statements import DEFAULT_LIMIT, MAX_LIMIT, StatementPage
from tests.factories import get_settlement_account

ENTRY_FIELDS = {
    "entry_id",
    "transaction_id",
    "kind",
    "amount_minor",
    "balance_after_minor",
    "created_at",
}
NOT_FOUND = {"code": "account_not_found", "detail": "Account not found."}


async def _customer(client: AsyncClient, currency: str = "GBP") -> tuple[str, str]:
    response = await client.post("/users", json={"email": f"u-{uuid.uuid7().hex}@example.com"})
    assert response.status_code == 201, response.text
    user_id: str = response.json()["id"]
    response = await client.post(f"/users/{user_id}/accounts", json={"currency": currency})
    assert response.status_code == 201, response.text
    account_id: str = response.json()["id"]
    return user_id, account_id


async def _post(
    client: AsyncClient, user_id: str, account_id: str, operation: str, body: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post(
        f"/users/{user_id}/accounts/{account_id}/{operation}",
        json=body,
        headers={"Idempotency-Key": f"key-{uuid.uuid7().hex}"},
    )
    assert response.status_code == 201, response.text
    posted: dict[str, Any] = response.json()
    return posted


async def _statement(
    client: AsyncClient, user_id: str, account_id: str, **params: Any
) -> tuple[int, dict[str, Any]]:
    response = await client.get(f"/users/{user_id}/accounts/{account_id}/statement", params=params)
    return response.status_code, response.json()


async def _all_pages(
    client: AsyncClient, user_id: str, account_id: str, limit: int
) -> list[dict[str, Any]]:
    pages, cursor = [], None
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        status_code, page = await _statement(client, user_id, account_id, **params)
        assert status_code == 200, page
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


# --- the statement ------------------------------------------------------------------------------


async def test_an_account_without_entries_has_an_empty_statement(api_client: AsyncClient) -> None:
    user_id, account_id = await _customer(api_client)

    status_code, body = await _statement(api_client, user_id, account_id)

    assert status_code == 200
    assert body == {
        "account_id": account_id,
        "balance_minor": 0,
        "entries": [],
        "next_cursor": None,
    }


async def test_entries_are_newest_first_signed_with_running_balances(
    api_client: AsyncClient,
) -> None:
    user_id, account_id = await _customer(api_client)
    other_user, other_account = await _customer(api_client)
    await _post(api_client, other_user, other_account, "deposits", {"amount_minor": 500})
    deposit = await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 1_000})
    withdrawal = await _post(api_client, user_id, account_id, "withdrawals", {"amount_minor": 300})
    sent = await _post(
        api_client,
        user_id,
        account_id,
        "transfers",
        {"destination_account_id": other_account, "amount_minor": 200},
    )
    received = await _post(
        api_client,
        other_user,
        other_account,
        "transfers",
        {"destination_account_id": account_id, "amount_minor": 50},
    )

    status_code, body = await _statement(api_client, user_id, account_id)

    assert status_code == 200
    assert (body["account_id"], body["balance_minor"], body["next_cursor"]) == (
        account_id,
        550,
        None,
    )
    assert all(set(entry) == ENTRY_FIELDS for entry in body["entries"])  # no counterparty
    assert [
        (e["transaction_id"], e["kind"], e["amount_minor"], e["balance_after_minor"])
        for e in body["entries"]
    ] == [
        (received["transaction_id"], "transfer", 50, 550),  # credited by someone else's transfer
        (sent["transaction_id"], "transfer", -200, 500),
        (withdrawal["transaction_id"], "withdrawal", -300, 700),
        (deposit["transaction_id"], "deposit", 1_000, 1_000),
    ]
    # The newest entry's running balance is the current balance.
    assert body["entries"][0]["balance_after_minor"] == body["balance_minor"]


async def test_a_payee_sees_only_its_own_side_of_a_transfer(api_client: AsyncClient) -> None:
    payer_user, payer_account = await _customer(api_client)
    payee_user, payee_account = await _customer(api_client)
    await _post(api_client, payer_user, payer_account, "deposits", {"amount_minor": 1_000})
    await _post(
        api_client,
        payer_user,
        payer_account,
        "transfers",
        {"destination_account_id": payee_account, "amount_minor": 400},
    )

    _, body = await _statement(api_client, payee_user, payee_account)

    [entry] = body["entries"]
    assert (entry["amount_minor"], entry["balance_after_minor"]) == (400, 400)
    assert payer_account not in str(body)  # nothing names the payer's account
    assert payer_user not in str(body)


# --- pagination ---------------------------------------------------------------------------------


async def test_following_the_cursor_returns_every_entry_exactly_once(
    api_client: AsyncClient,
) -> None:
    user_id, account_id = await _customer(api_client)
    for amount in range(1, 8):  # seven deposits: 1, 2, ..., 7
        await _post(api_client, user_id, account_id, "deposits", {"amount_minor": amount})
    _, full = await _statement(api_client, user_id, account_id, limit=MAX_LIMIT)

    pages = await _all_pages(api_client, user_id, account_id, limit=3)

    assert [len(page["entries"]) for page in pages] == [3, 3, 1]
    assert [page["next_cursor"] is None for page in pages] == [False, False, True]
    walked = [entry for page in pages for entry in page["entries"]]
    assert walked == full["entries"]  # same entries, same order, same running balances
    assert [entry["amount_minor"] for entry in walked] == [7, 6, 5, 4, 3, 2, 1]
    assert [entry["balance_after_minor"] for entry in walked] == [28, 21, 15, 10, 6, 3, 1]
    assert {page["balance_minor"] for page in pages} == {28}
    # The cursor is the last entry of the page it came from.
    assert pages[0]["next_cursor"] == pages[0]["entries"][-1]["entry_id"]


async def test_a_posting_between_pages_only_appears_on_a_fresh_first_page(
    api_client: AsyncClient,
) -> None:
    user_id, account_id = await _customer(api_client)
    for amount in (10, 20, 30):
        await _post(api_client, user_id, account_id, "deposits", {"amount_minor": amount})
    _, first = await _statement(api_client, user_id, account_id, limit=2)

    late = await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 40})
    _, second = await _statement(
        api_client, user_id, account_id, limit=2, cursor=first["next_cursor"]
    )
    _, fresh = await _statement(api_client, user_id, account_id, limit=2)

    assert [e["amount_minor"] for e in second["entries"]] == [10]  # nothing repeated or skipped
    assert second["next_cursor"] is None
    assert fresh["entries"][0]["transaction_id"] == late["transaction_id"]


async def test_the_default_page_size_applies_without_a_limit(api_client: AsyncClient) -> None:
    user_id, account_id = await _customer(api_client)
    for _ in range(DEFAULT_LIMIT + 1):
        await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 1})

    _, body = await _statement(api_client, user_id, account_id)

    assert len(body["entries"]) == DEFAULT_LIMIT
    assert body["next_cursor"] is not None


@pytest.mark.parametrize("limit", [1, MAX_LIMIT])
async def test_the_page_size_bounds_are_accepted(api_client: AsyncClient, limit: int) -> None:
    user_id, account_id = await _customer(api_client)
    await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 1})

    status_code, body = await _statement(api_client, user_id, account_id, limit=limit)

    assert status_code == 200
    assert len(body["entries"]) == 1


@pytest.mark.parametrize("limit", ["0", "-1", str(MAX_LIMIT + 1), "9" * 30])
async def test_an_out_of_range_page_size_is_invalid_statement_limit(
    api_client: AsyncClient, limit: str
) -> None:
    user_id, account_id = await _customer(api_client)

    status_code, body = await _statement(api_client, user_id, account_id, limit=limit)

    assert (status_code, body["code"]) == (422, "invalid_statement_limit")


@pytest.mark.parametrize("limit", ["abc", "1.5", "", "true"])
async def test_a_non_integer_page_size_is_a_validation_error(
    api_client: AsyncClient, limit: str
) -> None:
    user_id, account_id = await _customer(api_client)

    status_code, body = await _statement(api_client, user_id, account_id, limit=limit)

    assert (status_code, body["code"]) == (422, "validation_error")
    assert all(set(error) <= {"type", "loc", "msg"} for error in body["detail"])  # no echo


@pytest.mark.parametrize("cursor", ["not-a-uuid", "123", ""])
async def test_a_malformed_cursor_is_a_validation_error(
    api_client: AsyncClient, cursor: str
) -> None:
    user_id, account_id = await _customer(api_client)

    status_code, body = await _statement(api_client, user_id, account_id, cursor=cursor)

    assert (status_code, body["code"]) == (422, "validation_error")


async def test_an_unknown_or_foreign_cursor_is_invalid_statement_cursor(
    api_client: AsyncClient,
) -> None:
    user_id, account_id = await _customer(api_client)
    other_user, other_account = await _customer(api_client)
    await _post(api_client, user_id, account_id, "deposits", {"amount_minor": 1})
    await _post(api_client, other_user, other_account, "deposits", {"amount_minor": 1})
    _, theirs = await _statement(api_client, other_user, other_account)
    foreign_entry = theirs["entries"][0]["entry_id"]

    for cursor in (str(uuid.uuid7()), foreign_entry):
        status_code, body = await _statement(api_client, user_id, account_id, cursor=cursor)
        # Unknown and someone else's entry are indistinguishable.
        assert (status_code, body) == (
            422,
            {
                "code": "invalid_statement_cursor",
                "detail": "The statement cursor is not valid for this account.",
            },
        )


# --- ownership ----------------------------------------------------------------------------------


async def test_missing_foreign_and_system_accounts_are_the_same_404(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id, account_id = await _customer(api_client)
    other_user, other_account = await _customer(api_client)
    await _post(api_client, other_user, other_account, "deposits", {"amount_minor": 999})
    settlement = await get_settlement_account(db_session, Currency.GBP)

    for owner, account in [
        (user_id, str(uuid.uuid7())),  # missing
        (user_id, other_account),  # someone else's
        (user_id, str(settlement.id)),  # the system settlement account
        (str(uuid.uuid7()), account_id),  # an unknown user
    ]:
        status_code, body = await _statement(api_client, owner, account)
        assert (status_code, body) == (404, NOT_FOUND), (owner, account)


async def test_ownership_is_checked_before_the_cursor(api_client: AsyncClient) -> None:
    # A bad cursor on someone else's account must not reveal that the account exists.
    user_id, _ = await _customer(api_client)
    _, other_account = await _customer(api_client)

    status_code, body = await _statement(
        api_client, user_id, other_account, cursor=str(uuid.uuid7())
    )

    assert (status_code, body) == (404, NOT_FOUND)


async def test_an_inconsistent_account_is_a_generic_500(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def inconsistent(*args: Any, **kwargs: Any) -> StatementPage:
        raise StatementInconsistencyError

    monkeypatch.setattr(accounts_routes, "get_statement", inconsistent)
    user_id, account_id = await _customer(api_client)

    status_code, body = await _statement(api_client, user_id, account_id)

    assert (status_code, body) == (
        500,
        {"code": "internal_error", "detail": "Internal server error."},
    )


# --- OpenAPI ------------------------------------------------------------------------------------


async def test_openapi_documents_the_statement(api_client: AsyncClient) -> None:
    schema = (await api_client.get("/openapi.json")).json()
    path = schema["paths"]["/users/{user_id}/accounts/{account_id}/statement"]

    assert set(path) == {"get"}
    operation = path["get"]
    query = {p["name"]: p for p in operation["parameters"] if p["in"] == "query"}
    assert set(query) == {"limit", "cursor"}
    assert query["limit"]["schema"]["default"] == DEFAULT_LIMIT
    assert {"200", "404", "422", "500"} <= set(operation["responses"])
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/StatementResponse"}
    components = schema["components"]["schemas"]
    assert set(components["StatementResponse"]["properties"]) == {
        "account_id",
        "balance_minor",
        "entries",
        "next_cursor",
    }
    assert set(components["StatementEntryResponse"]["properties"]) == ENTRY_FIELDS


async def test_no_balance_or_transactions_list_endpoint_was_added(api_client: AsyncClient) -> None:
    paths = (await api_client.get("/openapi.json")).json()["paths"]

    assert not any(path.endswith(("/balance", "/transactions")) for path in paths)
