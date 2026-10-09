"""Audit events: what was attempted, by whom, and how it ended (ADR 0011). Pure: no I/O.

An AuditRecord mirrors the CHECK constraints of audit_events, so an inconsistent event is a
programming error caught before any I/O; the database remains the authority.
"""

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class AuditAction(StrEnum):
    USER_REGISTER = "user.register"
    ACCOUNT_OPEN = "account.open"
    POSTING_DEPOSIT = "posting.deposit"
    POSTING_WITHDRAWAL = "posting.withdrawal"
    POSTING_TRANSFER = "posting.transfer"


# A successful posting, and only a successful posting, references its transaction.
POSTING_ACTIONS = frozenset(
    {AuditAction.POSTING_DEPOSIT, AuditAction.POSTING_WITHDRAWAL, AuditAction.POSTING_TRANSFER}
)


class AuditOutcome(StrEnum):
    # Committed together with the business operation, in the same database transaction.
    SUCCEEDED = "succeeded"
    # A business rule refused the request (reason: the DomainError code).
    REJECTED = "rejected"
    # A system error: contention, a bug, a lost connection.
    FAILED = "failed"


# Failure reasons that are not DomainError codes.
INTERNAL_ERROR = "internal_error"

# Stable, machine-readable codes: the same shape as DomainError.code.
REASON_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Upper bound on the JSON text of `details` (ck_audit_events_details_size). Details are a few
# identifiers and numbers; the bound stops an event from becoming a data dump.
MAX_DETAILS_BYTES = 2048

# Added by the persistence layer when the claimed actor is not a known user (ADR 0011).
REQUESTED_USER_ID = "requested_user_id"

# Set on failed postings: the id the posting's transaction header was given before it failed.
# If that id exists after all, the COMMIT succeeded and only its acknowledgement was lost
# (reconciliation: find_failed_events_for_committed_transactions).
ATTEMPTED_TRANSACTION_ID = "attempted_transaction_id"

# JSON scalars only: no nested structures, no arbitrary objects, nothing that could smuggle in
# an exception message or a request body. UUIDs are stored as their string form.
type DetailValue = str | int | bool | uuid.UUID | None


def _json_scalar(value: DetailValue) -> str | int | bool | None:
    if isinstance(value, uuid.UUID | str):
        return str(value)  # str() also turns a StrEnum (e.g. Currency) into its plain value
    if value is None or type(value) in (int, bool):
        return value
    raise ValueError(f"audit detail values must be JSON scalars, not {type(value).__name__}")


def details_size(details: Mapping[str, object]) -> int:
    """Bytes of the JSON text, as PostgreSQL's details::text renders it (", " and ": ")."""
    return len(json.dumps(details, ensure_ascii=False).encode())


@dataclass(frozen=True, slots=True)
class AuditRecord:
    action: AuditAction
    outcome: AuditOutcome
    # The user on whose behalf the request ran, as claimed by the caller. Until authentication
    # exists (Phase 8) it may not be a real user; persistence then stores NULL (ADR 0011).
    actor_user_id: uuid.UUID | None = None
    reason: str | None = None
    transaction_id: uuid.UUID | None = None
    details: Mapping[str, DetailValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Inconsistent events are programming errors, like a malformed Posting: ValueError.
        succeeded = self.outcome == AuditOutcome.SUCCEEDED
        if succeeded != (self.reason is None):
            raise ValueError("an audit event has a reason exactly when it did not succeed")
        if self.reason is not None and not REASON_PATTERN.fullmatch(self.reason):
            raise ValueError("an audit reason must be a stable snake_case code")
        if (self.transaction_id is not None) != (succeeded and self.action in POSTING_ACTIONS):
            raise ValueError("only a successful posting references a transaction")
        if succeeded and self.actor_user_id is None:
            raise ValueError("a successful operation always has an actor")

        details = {key: _json_scalar(value) for key, value in self.details.items()}
        if not all(isinstance(key, str) for key in details):
            raise ValueError("audit detail keys must be strings")
        if REQUESTED_USER_ID in details:
            raise ValueError(f"{REQUESTED_USER_ID} is reserved for the persistence layer")
        # Measured with the key persistence may add, so the database bound can never be hit.
        worst_case = {**details, REQUESTED_USER_ID: str(self.actor_user_id)}
        if details_size(worst_case) > MAX_DETAILS_BYTES:
            raise ValueError(f"audit details exceed {MAX_DETAILS_BYTES} bytes")
        object.__setattr__(self, "details", MappingProxyType(details))
