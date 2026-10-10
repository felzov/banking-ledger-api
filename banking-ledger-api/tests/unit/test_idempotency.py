"""Idempotency key format and request fingerprints (ADR 0006). Pure."""

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledger_api.domain.idempotency import (
    FINGERPRINT_VERSION,
    is_valid_idempotency_key,
    request_fingerprint,
)
from ledger_api.domain.posting import MAX_AMOUNT_MINOR
from ledger_api.domain.transaction import TransactionKind

# --- key format --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["k", "!", "~", "K" * 255, str(uuid.uuid4()), "order:2026-10-10/#1?x=y&z"]
)
def test_visible_ascii_keys_of_1_to_255_characters_are_valid(key: str) -> None:
    assert is_valid_idempotency_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "K" * 256,
        " ",
        "with space",
        "tab\tkey",
        "line\nbreak",
        "trailing\n",  # re's $ would accept this; fullmatch does not
        "del\x7f",
        "nul\x00",
        "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
        "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}",
        "\N{FULLWIDTH LATIN CAPITAL LETTER A}",
    ],
)
def test_other_keys_are_invalid(key: str) -> None:
    assert not is_valid_idempotency_key(key)


@pytest.mark.parametrize("key", [None, 1, b"key", ["key"]])
def test_non_strings_are_invalid(key: object) -> None:
    assert not is_valid_idempotency_key(key)


@given(st.text(alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E), min_size=1))
def test_any_visible_ascii_text_up_to_255_characters_is_valid(key: str) -> None:
    assert is_valid_idempotency_key(key) == (len(key) <= 255)


# --- fingerprint -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    operation: TransactionKind
    user_id: uuid.UUID
    account_id: uuid.UUID
    destination_account_id: uuid.UUID | None
    amount_minor: int

    def fingerprint(self) -> bytes:
        return request_fingerprint(
            operation=self.operation,
            user_id=self.user_id,
            account_id=self.account_id,
            destination_account_id=self.destination_account_id,
            amount_minor=self.amount_minor,
        )


ids = st.uuids()
amounts = st.integers(min_value=1, max_value=MAX_AMOUNT_MINOR)
requests = st.one_of(
    st.builds(
        Request,
        operation=st.sampled_from([TransactionKind.DEPOSIT, TransactionKind.WITHDRAWAL]),
        user_id=ids,
        account_id=ids,
        destination_account_id=st.none(),
        amount_minor=amounts,
    ),
    st.builds(
        Request,
        operation=st.just(TransactionKind.TRANSFER),
        user_id=ids,
        account_id=ids,
        destination_account_id=ids,
        amount_minor=amounts,
    ),
)


@given(requests)
def test_fingerprint_is_deterministic_sha256(request: Request) -> None:
    fingerprint = request.fingerprint()

    assert fingerprint == request.fingerprint()
    assert len(fingerprint) == 32


@given(first=requests, second=requests)
def test_requests_that_differ_in_any_field_have_different_fingerprints(
    first: Request, second: Request
) -> None:
    assert (first.fingerprint() == second.fingerprint()) == (first == second)


TRANSFER = Request(TransactionKind.TRANSFER, uuid.uuid7(), uuid.uuid7(), uuid.uuid7(), 100)
DEPOSIT = replace(TRANSFER, operation=TransactionKind.DEPOSIT, destination_account_id=None)


@pytest.mark.parametrize(
    "changed",
    [
        replace(DEPOSIT, operation=TransactionKind.WITHDRAWAL),
        replace(DEPOSIT, user_id=uuid.uuid7()),
        replace(DEPOSIT, account_id=uuid.uuid7()),
        replace(DEPOSIT, amount_minor=101),
    ],
    ids=["operation", "user", "account", "amount"],
)
def test_each_field_of_a_deposit_changes_its_fingerprint(changed: Request) -> None:
    assert changed.fingerprint() != DEPOSIT.fingerprint()


def test_a_transfers_destination_changes_its_fingerprint() -> None:
    other = replace(TRANSFER, destination_account_id=uuid.uuid7())

    assert other.fingerprint() != TRANSFER.fingerprint()


def test_swapping_source_and_destination_changes_the_fingerprint() -> None:
    assert TRANSFER.destination_account_id is not None
    swapped = replace(
        TRANSFER,
        account_id=TRANSFER.destination_account_id,
        destination_account_id=TRANSFER.account_id,
    )

    assert swapped.fingerprint() != TRANSFER.fingerprint()


def test_fingerprint_is_the_documented_canonical_form() -> None:
    # Pinned so the canonical form cannot change silently: that would make every stored
    # fingerprint mismatch its own retries. Changing it means bumping FINGERPRINT_VERSION.
    expected: dict[str, Any] = {
        "account_id": str(TRANSFER.account_id),
        "amount_minor": 100,
        "destination_account_id": str(TRANSFER.destination_account_id),
        "operation": "transfer",
        "user_id": str(TRANSFER.user_id),
        "v": FINGERPRINT_VERSION,
    }
    canonical = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()

    assert TRANSFER.fingerprint() == hashlib.sha256(canonical).digest()
    assert FINGERPRINT_VERSION == 1


@pytest.mark.parametrize(
    ("operation", "destination"),
    [
        (TransactionKind.TRANSFER, None),
        (TransactionKind.DEPOSIT, uuid.uuid7()),
        (TransactionKind.WITHDRAWAL, uuid.uuid7()),
    ],
)
def test_destination_is_required_for_and_only_for_transfers(
    operation: TransactionKind, destination: uuid.UUID | None
) -> None:
    with pytest.raises(ValueError, match="destination"):
        replace(DEPOSIT, operation=operation, destination_account_id=destination).fingerprint()


@pytest.mark.parametrize("amount", [True, 100.0, "100"])
def test_amount_must_be_a_real_int(amount: Any) -> None:
    with pytest.raises(ValueError, match="amount_minor"):
        replace(DEPOSIT, amount_minor=amount).fingerprint()
