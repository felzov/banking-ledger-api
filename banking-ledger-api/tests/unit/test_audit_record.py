"""AuditRecord mirrors the audit_events CHECK constraints before any I/O (ADR 0011)."""

import uuid

import pytest

from ledger_api.domain.audit import (
    MAX_DETAILS_BYTES,
    REQUESTED_USER_ID,
    AuditAction,
    AuditOutcome,
    AuditRecord,
    DetailValue,
)
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import InsufficientFundsError
from ledger_api.services.audit import AuditContext

ACTOR = uuid.uuid7()
TRANSACTION = uuid.uuid7()


def test_successful_posting_references_its_transaction() -> None:
    record = AuditRecord(
        AuditAction.POSTING_DEPOSIT,
        AuditOutcome.SUCCEEDED,
        actor_user_id=ACTOR,
        transaction_id=TRANSACTION,
    )

    assert record.reason is None


def test_rejection_without_an_actor_is_valid() -> None:
    record = AuditRecord(
        AuditAction.USER_REGISTER, AuditOutcome.REJECTED, reason="email_already_registered"
    )

    assert record.actor_user_id is None


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (AuditOutcome.SUCCEEDED, "insufficient_funds"),
        (AuditOutcome.REJECTED, None),
        (AuditOutcome.FAILED, None),
    ],
)
def test_reason_is_present_exactly_when_unsuccessful(
    outcome: AuditOutcome, reason: str | None
) -> None:
    with pytest.raises(ValueError, match="reason"):
        AuditRecord(AuditAction.ACCOUNT_OPEN, outcome, actor_user_id=ACTOR, reason=reason)


@pytest.mark.parametrize("reason", ["", "Insufficient", "has space", "x" * 65, "1abc"])
def test_reason_must_be_a_stable_code(reason: str) -> None:
    with pytest.raises(ValueError, match="snake_case"):
        AuditRecord(AuditAction.ACCOUNT_OPEN, AuditOutcome.REJECTED, reason=reason)


def test_successful_posting_requires_a_transaction() -> None:
    with pytest.raises(ValueError, match="transaction"):
        AuditRecord(AuditAction.POSTING_TRANSFER, AuditOutcome.SUCCEEDED, actor_user_id=ACTOR)


@pytest.mark.parametrize(
    ("action", "outcome", "reason"),
    [
        (AuditAction.ACCOUNT_OPEN, AuditOutcome.SUCCEEDED, None),
        (AuditAction.POSTING_WITHDRAWAL, AuditOutcome.REJECTED, "insufficient_funds"),
        (AuditAction.POSTING_WITHDRAWAL, AuditOutcome.FAILED, "internal_error"),
    ],
)
def test_only_successful_postings_reference_a_transaction(
    action: AuditAction, outcome: AuditOutcome, reason: str | None
) -> None:
    with pytest.raises(ValueError, match="transaction"):
        AuditRecord(action, outcome, actor_user_id=ACTOR, reason=reason, transaction_id=TRANSACTION)


def test_success_requires_an_actor() -> None:
    with pytest.raises(ValueError, match="actor"):
        AuditRecord(AuditAction.ACCOUNT_OPEN, AuditOutcome.SUCCEEDED)


def test_details_are_normalised_to_json_scalars_and_frozen() -> None:
    account_id = uuid.uuid7()
    record = AuditRecord(
        AuditAction.POSTING_WITHDRAWAL,
        AuditOutcome.REJECTED,
        reason="insufficient_funds",
        details={
            "account_id": account_id,
            "amount_minor": 500,
            "retried": False,
            "x": None,
            "currency": Currency.GBP,
        },
    )

    assert type(record.details["currency"]) is str
    assert dict(record.details) == {
        "account_id": str(account_id),
        "currency": "GBP",
        "amount_minor": 500,
        "retried": False,
        "x": None,
    }
    with pytest.raises(TypeError):
        record.details["amount_minor"] = 1  # type: ignore[index]


@pytest.mark.parametrize("value", [1.5, [1], {"nested": 1}, b"bytes", ValueError("secret")])
def test_details_reject_non_scalar_values(value: object) -> None:
    details: dict[str, DetailValue] = {"value": value}  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="JSON scalars"):
        AuditRecord(AuditAction.ACCOUNT_OPEN, AuditOutcome.FAILED, reason="x", details=details)


def test_requested_user_id_is_reserved_for_persistence() -> None:
    with pytest.raises(ValueError, match="reserved"):
        AuditRecord(
            AuditAction.ACCOUNT_OPEN,
            AuditOutcome.REJECTED,
            reason="user_not_found",
            details={REQUESTED_USER_ID: "x"},
        )


def test_details_are_size_limited() -> None:
    with pytest.raises(ValueError, match=str(MAX_DETAILS_BYTES)):
        AuditRecord(
            AuditAction.ACCOUNT_OPEN,
            AuditOutcome.REJECTED,
            reason="x",
            details={"blob": "x" * MAX_DETAILS_BYTES},
        )


# --- idempotency keys (ADR 0006) ---------------------------------------------------------------


@pytest.mark.parametrize("key", ["k", "K" * 255, "order:42/retry#1"])
def test_a_valid_idempotency_key_is_kept(key: str) -> None:
    record = AuditRecord(
        AuditAction.POSTING_DEPOSIT,
        AuditOutcome.REJECTED,
        actor_user_id=ACTOR,
        reason="insufficient_funds",
        idempotency_key=key,
    )

    assert record.idempotency_key == key
    assert "idempotency_key" not in record.details


@pytest.mark.parametrize(
    "key", ["", "K" * 256, "with space", "caf\N{LATIN SMALL LETTER E WITH ACUTE}"]
)
def test_an_invalid_idempotency_key_is_a_programming_error(key: str) -> None:
    with pytest.raises(ValueError, match="idempotency key"):
        AuditRecord(
            AuditAction.POSTING_DEPOSIT,
            AuditOutcome.REJECTED,
            actor_user_id=ACTOR,
            reason="insufficient_funds",
            idempotency_key=key,
        )


def test_an_audit_context_records_its_key_on_every_outcome() -> None:
    context = AuditContext(AuditAction.POSTING_DEPOSIT, ACTOR, idempotency_key="deposit-7")

    records = [
        context.succeeded(transaction_id=TRANSACTION),
        context.rejected(InsufficientFundsError()),
        context.failed(RuntimeError("boom")),
    ]

    assert [record.idempotency_key for record in records] == ["deposit-7"] * 3
