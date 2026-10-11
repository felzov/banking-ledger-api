"""The OpenAPI contract describes the 422 bodies the API actually returns.

Two different bodies share status 422: FastAPI's validation_error ({code, detail: [{type, loc,
msg}]}) and a business rule or header check refusing a well-formed request ({code, detail:
string}). Each test sends a real request, takes the route's documented 422 schema from
/openapi.json, and validates the real body against it.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import BaseModel, ValidationError

from ledger_api.api.errors import ErrorResponse, ValidationErrorResponse
from tests.conftest import TEST_PASSWORD

MODELS: dict[str, type[BaseModel]] = {
    "#/components/schemas/ErrorResponse": ErrorResponse,
    "#/components/schemas/ValidationErrorResponse": ValidationErrorResponse,
}

# Routes whose service can refuse a well-formed request with 422: both bodies are documented.
RULE_422_ROUTES = {
    ("post", "/users/{user_id}/accounts/{account_id}/deposits"),
    ("post", "/users/{user_id}/accounts/{account_id}/withdrawals"),
    ("post", "/users/{user_id}/accounts/{account_id}/transfers"),
    ("get", "/users/{user_id}/accounts/{account_id}/statement"),
}


def _documented_422_models(schema: dict[str, Any], method: str, path: str) -> list[type[BaseModel]]:
    response = schema["paths"][path][method]["responses"]["422"]
    body = response["content"]["application/json"]["schema"]
    refs = [option["$ref"] for option in body["anyOf"]] if "anyOf" in body else [body["$ref"]]
    return [MODELS[ref] for ref in refs]


def _validates(model: type[BaseModel], body: Any) -> bool:
    try:
        model.model_validate(body)
    except ValidationError:
        return False
    return True


async def _customer(client: AsyncClient) -> tuple[str, str]:
    user = (
        await client.post(
            "/users", json={"email": f"u-{uuid.uuid7().hex}@example.com", "password": TEST_PASSWORD}
        )
    ).json()
    account = (await client.post(f"/users/{user['id']}/accounts", json={"currency": "GBP"})).json()
    return user["id"], account["id"]


def _key() -> dict[str, str]:
    return {"Idempotency-Key": f"key-{uuid.uuid7().hex}"}


async def test_the_documented_422_schemas_are_the_expected_ones(api_client: AsyncClient) -> None:
    schema = (await api_client.get("/openapi.json")).json()

    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if "422" not in operation["responses"]:
                continue
            documented = set(_documented_422_models(schema, method, path))
            if (method, path) in RULE_422_ROUTES:
                assert documented == {ValidationErrorResponse, ErrorResponse}, (method, path)
            else:
                # Only FastAPI's validation can produce a 422 here.
                assert documented == {ValidationErrorResponse}, (method, path)


@pytest.mark.parametrize(
    ("case", "expected_code", "expected_model"),
    [
        ("deposit-invalid-amount", "invalid_amount", ErrorResponse),
        ("deposit-malformed-amount", "validation_error", ValidationErrorResponse),
        ("deposit-invalid-key", "invalid_idempotency_key", ErrorResponse),
        ("withdrawal-insufficient-funds", "insufficient_funds", ErrorResponse),
        ("transfer-same-account", "same_account_transfer", ErrorResponse),
        ("transfer-missing-destination", "validation_error", ValidationErrorResponse),
        ("statement-limit-out-of-range", "invalid_statement_limit", ErrorResponse),
        ("statement-limit-not-an-integer", "validation_error", ValidationErrorResponse),
        ("statement-unknown-cursor", "invalid_statement_cursor", ErrorResponse),
        ("register-invalid-email", "validation_error", ValidationErrorResponse),
        ("transaction-malformed-id", "validation_error", ValidationErrorResponse),
    ],
)
async def test_every_real_422_body_matches_its_documented_schema(
    api_client: AsyncClient, case: str, expected_code: str, expected_model: type[BaseModel]
) -> None:
    user_id, account_id = await _customer(api_client)
    base = f"/users/{user_id}/accounts/{account_id}"
    requests: dict[str, tuple[str, str, dict[str, Any]]] = {
        "deposit-invalid-amount": (
            "post",
            f"{base}/deposits",
            {"json": {"amount_minor": 0}, "headers": _key()},
        ),
        "deposit-malformed-amount": (
            "post",
            f"{base}/deposits",
            {"json": {"amount_minor": "x"}, "headers": _key()},
        ),
        "deposit-invalid-key": (
            "post",
            f"{base}/deposits",
            {"json": {"amount_minor": 1}, "headers": {"Idempotency-Key": "with space"}},
        ),
        "withdrawal-insufficient-funds": (
            "post",
            f"{base}/withdrawals",
            {"json": {"amount_minor": 1}, "headers": _key()},
        ),
        "transfer-same-account": (
            "post",
            f"{base}/transfers",
            {
                "json": {"destination_account_id": account_id, "amount_minor": 1},
                "headers": _key(),
            },
        ),
        "transfer-missing-destination": (
            "post",
            f"{base}/transfers",
            {"json": {"amount_minor": 1}, "headers": _key()},
        ),
        "statement-limit-out-of-range": ("get", f"{base}/statement", {"params": {"limit": 0}}),
        "statement-limit-not-an-integer": (
            "get",
            f"{base}/statement",
            {"params": {"limit": "abc"}},
        ),
        "statement-unknown-cursor": (
            "get",
            f"{base}/statement",
            {"params": {"cursor": str(uuid.uuid7())}},
        ),
        "register-invalid-email": ("post", "/users", {"json": {"email": "not-an-email"}}),
        "transaction-malformed-id": ("get", f"{base}/transactions/not-a-uuid", {}),
    }
    method, url, options = requests[case]
    route = {
        "deposit": "/users/{user_id}/accounts/{account_id}/deposits",
        "withdrawal": "/users/{user_id}/accounts/{account_id}/withdrawals",
        "transfer": "/users/{user_id}/accounts/{account_id}/transfers",
        "statement": "/users/{user_id}/accounts/{account_id}/statement",
        "register": "/users",
        "transaction": "/users/{user_id}/accounts/{account_id}/transactions/{transaction_id}",
    }[case.split("-")[0]]

    response = await api_client.request(method, url, **options)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == expected_code
    schema = (await api_client.get("/openapi.json")).json()
    documented = _documented_422_models(schema, method, route)
    # The real body is one of the documented shapes, and exactly the expected one.
    assert expected_model in documented
    assert _validates(expected_model, body)
    other = ErrorResponse if expected_model is ValidationErrorResponse else ValidationErrorResponse
    assert not _validates(other, body)  # the two shapes are genuinely distinct
