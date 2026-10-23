"""AuditRecord mirrors the audit_events CHECK constraints before any I/O (ADR 0011)."""

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from ledger_api.domain.audit import (
    MAX_DETAILS_BYTES,
    REQUESTED_USER_ID,
    AuditAction,
    AuditOutcome,
    AuditRecord,
    DetailValue,
)
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import (
    CommitOutcomeUnknownError,
    InsufficientFundsError,
    TemporarilyUnavailableError,
)
from ledger_api.observability.correlation import bind_request_id, unbind_request_id
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


CONNECTION_DETAILS = "connection to 10.0.0.9:5432 as ledger_app reset by peer"


def _caused(error: Exception, cause: BaseException) -> Exception:
    error.__cause__ = cause
    return error


def test_an_unknown_outcome_keeps_the_type_of_a_non_database_cause() -> None:
    context = AuditContext(AuditAction.POSTING_WITHDRAWAL, ACTOR, idempotency_key="k-1")
    error = _caused(CommitOutcomeUnknownError(), ConnectionResetError(CONNECTION_DETAILS))

    record = context.failed(error)

    assert record.reason == "outcome_unknown"
    assert record.details["error_type"] == "CommitOutcomeUnknownError"
    assert record.details["cause_type"] == "ConnectionResetError"
    assert "sqlstate" not in record.details  # nothing database-specific is invented
    # The class name only: never the message, which can quote connection details.
    assert all(CONNECTION_DETAILS not in str(value) for value in record.details.values())


def test_a_contention_error_records_its_database_cause_type_and_sqlstate() -> None:
    class _DriverError(Exception):
        sqlstate = "55P03"

    cause = DBAPIError("INSERT ...", None, _DriverError(CONNECTION_DETAILS))
    context = AuditContext(AuditAction.POSTING_DEPOSIT, ACTOR)

    record = context.failed(_caused(TemporarilyUnavailableError(), cause))

    assert record.details["cause_type"] == "DBAPIError"
    assert record.details["sqlstate"] == "55P03"
    assert all(CONNECTION_DETAILS not in str(value) for value in record.details.values())


def test_errors_without_a_cause_record_no_cause_type() -> None:
    context = AuditContext(AuditAction.POSTING_DEPOSIT, ACTOR)

    assert "cause_type" not in context.failed(RuntimeError(CONNECTION_DETAILS)).details
    assert "cause_type" not in context.failed(TemporarilyUnavailableError()).details


# --- the request id (ADR 0017) --------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_id",
    ["", "x" * 129, "has space", "line\nbreak", 'quote"d', "ünïcode"],
    ids=["empty", "too-long", "space", "newline", "quote", "non-ascii"],
)
def test_a_malformed_request_id_is_refused(request_id: str) -> None:
    with pytest.raises(ValueError, match="request id"):
        AuditRecord(
            AuditAction.USER_REGISTER,
            AuditOutcome.REJECTED,
            reason="email_already_registered",
            request_id=request_id,
        )


def test_an_audit_context_records_the_request_it_began_in_on_every_outcome() -> None:
    token = bind_request_id("0199f6c2-7b1e-7c3a-9d4e-5f6a7b8c9d0e")
    try:
        context = AuditContext(AuditAction.POSTING_DEPOSIT, ACTOR)
    finally:
        unbind_request_id(token)

    records = [
        context.succeeded(transaction_id=TRANSACTION),
        context.rejected(InsufficientFundsError()),
        context.failed(TemporarilyUnavailableError()),
    ]

    assert {record.request_id for record in records} == {"0199f6c2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"}


def test_outside_a_request_an_audit_context_has_no_request_id() -> None:
    # A service called directly (a test, a script): the column stays NULL.
    context = AuditContext(AuditAction.POSTING_DEPOSIT, ACTOR)

    assert context.rejected(InsufficientFundsError()).request_id is None
