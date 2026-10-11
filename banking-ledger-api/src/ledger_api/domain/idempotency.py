"""Idempotency keys and request fingerprints (ADR 0006, Phase 6 amendment). Pure: no I/O.

A key identifies one intended posting of one user. The fingerprint identifies WHAT was
intended: the same key with the same fingerprint is a retry (replayed), the same key with a
different fingerprint is a client error (idempotency_key_mismatch).

The fingerprint is computed from the canonical business fields, never from the raw HTTP body:
whitespace, field order and number spelling cannot make a retry look different, and every field
that changes the posting's meaning is covered.
"""

import hashlib
import json
import re
import uuid

from ledger_api.domain.transaction import TransactionKind

# Visible ASCII (0x21-0x7E), 1-255 characters: printable, unambiguous in logs and headers, and
# within the audit_events.idempotency_key bound. Mirrors ck_idempotency_keys_key_format.
KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,255}")

# Bumped if the canonical form ever changes, so old and new fingerprints can never collide.
FINGERPRINT_VERSION = 1


def is_valid_idempotency_key(key: object) -> bool:
    return type(key) is str and KEY_PATTERN.fullmatch(key) is not None


def request_fingerprint(
    *,
    operation: TransactionKind,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    destination_account_id: uuid.UUID | None,
    amount_minor: int,
) -> bytes:
    """SHA-256 of the canonical JSON of a posting request's business fields (32 bytes).

    Inputs are expected to be validated already (pre-validation runs first); anything malformed
    here is a programming error, hence ValueError.
    """
    if (operation == TransactionKind.TRANSFER) != (destination_account_id is not None):
        raise ValueError("a destination account is required for, and only for, transfers")
    # type() rather than isinstance(): bool is a subclass of int, and True is not an amount.
    if type(amount_minor) is not int:
        raise ValueError("amount_minor must be an int")
    canonical = json.dumps(
        {
            "v": FINGERPRINT_VERSION,
            "operation": str(operation),
            "user_id": str(user_id),
            "account_id": str(account_id),
            "destination_account_id": (
                None if destination_account_id is None else str(destination_account_id)
            ),
            "amount_minor": amount_minor,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("ascii")).digest()
