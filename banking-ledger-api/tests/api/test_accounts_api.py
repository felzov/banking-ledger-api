import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.domain.currency import Currency
from tests.factories import get_settlement_account


async def _create_user(client: AsyncClient) -> str:
    response = await client.post("/users", json={"email": f"u-{uuid.uuid7().hex}@example.com"})
    assert response.status_code == 201, response.text
    user_id: str = response.json()["id"]
    return user_id


async def _open_account(client: AsyncClient, user_id: str, currency: str) -> dict[str, Any]:
    response = await client.post(f"/users/{user_id}/accounts", json={"currency": currency})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# --- POST /users/{user_id}/accounts ------------------------------------------------------------


@pytest.mark.parametrize("currency", [currency.value for currency in Currency])
async def test_open_account_returns_201_with_an_empty_account(
    api_client: AsyncClient, currency: str
) -> None:
    user_id = await _create_user(api_client)

    response = await api_client.post(f"/users/{user_id}/accounts", json={"currency": currency})

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "currency", "balance_minor", "created_at"}
    assert body["currency"] == currency
    assert body["balance_minor"] == 0
    assert response.headers["location"] == f"/users/{user_id}/accounts/{body['id']}"


async def test_location_header_points_at_the_new_account(api_client: AsyncClient) -> None:
    user_id = await _create_user(api_client)
    created = await api_client.post(f"/users/{user_id}/accounts", json={"currency": "GBP"})

    fetched = await api_client.get(created.headers["location"])

    assert fetched.status_code == 200
    assert fetched.json() == created.json()


async def test_second_account_in_the_same_currency_returns_409(api_client: AsyncClient) -> None:
    user_id = await _create_user(api_client)
    await _open_account(api_client, user_id, "GBP")

    response = await api_client.post(f"/users/{user_id}/accounts", json={"currency": "GBP"})

    assert response.status_code == 409
    assert response.json() == {
        "code": "account_already_exists",
        "detail": "The user already has an account in this currency.",
    }


async def test_opening_an_account_for_an_unknown_user_returns_404(
    api_client: AsyncClient,
) -> None:
    response = await api_client.post(f"/users/{uuid.uuid7()}/accounts", json={"currency": "GBP"})

    assert response.status_code == 404
    assert response.json() == {"code": "user_not_found", "detail": "User not found."}


@pytest.mark.parametrize(
    "payload",
    [{}, {"currency": "USD"}, {"currency": "gbp"}, {"currency": None}, {"currency": 826}],
)
async def test_invalid_currency_returns_422(
    api_client: AsyncClient, payload: dict[str, Any]
) -> None:
    user_id = await _create_user(api_client)

    response = await api_client.post(f"/users/{user_id}/accounts", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize(
    "extra",
    [
        {"kind": "system"},  # privilege escalation into the settlement role
        {"balance_minor": 1_000_000},  # money created without a posting
        {"user_id": str(uuid.uuid7())},  # opening an account for someone else
        {"ledger_id": str(uuid.uuid7())},
    ],
)
async def test_client_controlled_fields_are_rejected_and_nothing_is_created(
    api_client: AsyncClient, extra: dict[str, Any]
) -> None:
    user_id = await _create_user(api_client)

    response = await api_client.post(
        f"/users/{user_id}/accounts", json={"currency": "GBP", **extra}
    )

    assert response.status_code == 422
    listed = await api_client.get(f"/users/{user_id}/accounts")
    assert listed.json() == {"items": []}


# --- GET /users/{user_id}/accounts[/{account_id}] ----------------------------------------------


async def test_list_returns_the_users_accounts_in_creation_order(api_client: AsyncClient) -> None:
    user_id = await _create_user(api_client)
    eur = await _open_account(api_client, user_id, "EUR")
    gbp = await _open_account(api_client, user_id, "GBP")

    response = await api_client.get(f"/users/{user_id}/accounts")

    assert response.status_code == 200
    assert response.json() == {"items": [eur, gbp]}


async def test_list_for_an_unknown_user_returns_404(api_client: AsyncClient) -> None:
    response = await api_client.get(f"/users/{uuid.uuid7()}/accounts")

    assert response.status_code == 404
    assert response.json()["code"] == "user_not_found"


async def test_get_account_returns_it_to_its_owner(api_client: AsyncClient) -> None:
    user_id = await _create_user(api_client)
    opened = await _open_account(api_client, user_id, "EUR")

    response = await api_client.get(f"/users/{user_id}/accounts/{opened['id']}")

    assert response.status_code == 200
    assert response.json() == opened


async def test_malformed_account_id_returns_422(api_client: AsyncClient) -> None:
    user_id = await _create_user(api_client)

    response = await api_client.get(f"/users/{user_id}/accounts/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --- BOLA: an account is only reachable through its owner --------------------------------------


async def test_another_users_account_is_indistinguishable_from_a_missing_one(
    api_client: AsyncClient,
) -> None:
    owner = await _create_user(api_client)
    intruder = await _create_user(api_client)
    account = await _open_account(api_client, owner, "GBP")

    foreign = await api_client.get(f"/users/{intruder}/accounts/{account['id']}")
    missing = await api_client.get(f"/users/{intruder}/accounts/{uuid.uuid7()}")

    assert foreign.status_code == missing.status_code == 404
    assert (
        foreign.json()
        == missing.json()
        == {
            "code": "account_not_found",
            "detail": "Account not found.",
        }
    )


async def test_list_never_includes_another_users_accounts(api_client: AsyncClient) -> None:
    owner = await _create_user(api_client)
    intruder = await _create_user(api_client)
    await _open_account(api_client, owner, "GBP")
    own = await _open_account(api_client, intruder, "EUR")

    response = await api_client.get(f"/users/{intruder}/accounts")

    assert response.json() == {"items": [own]}


async def test_system_account_is_not_reachable_through_any_user(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await _create_user(api_client)
    settlement = await get_settlement_account(db_session, Currency.GBP)

    response = await api_client.get(f"/users/{user_id}/accounts/{settlement.id}")

    assert response.status_code == 404
    assert response.json()["code"] == "account_not_found"


async def test_accounts_are_isolated_even_with_an_unknown_user_in_the_path(
    api_client: AsyncClient,
) -> None:
    owner = await _create_user(api_client)
    account = await _open_account(api_client, owner, "GBP")

    response = await api_client.get(f"/users/{uuid.uuid7()}/accounts/{account['id']}")

    assert response.status_code == 404
    assert response.json()["code"] == "account_not_found"
