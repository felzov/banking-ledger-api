import uuid

import pytest

from ledger_api.domain.errors import InvalidAmountError, SameAccountTransferError
from ledger_api.domain.posting import (
    MAX_AMOUNT_MINOR,
    Posting,
    PostingLine,
    deposit_posting,
    transfer_posting,
    validate_amount,
    withdrawal_posting,
)
from ledger_api.domain.transaction import TransactionKind

A, B, S = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()


# --- amounts -----------------------------------------------------------------------------------


@pytest.mark.parametrize("amount", [1, 1050, MAX_AMOUNT_MINOR])
def test_valid_amounts_are_accepted(amount: int) -> None:
    assert validate_amount(amount) == amount


@pytest.mark.parametrize(
    "amount",
    [0, -1, MAX_AMOUNT_MINOR + 1, True, False, 1.0, 10.5, "100", None],
    ids=repr,
)
def test_invalid_amounts_are_rejected(amount: object) -> None:
    with pytest.raises(InvalidAmountError):
        validate_amount(amount)  # type: ignore[arg-type]


# --- builders ----------------------------------------------------------------------------------


def test_deposit_moves_money_from_settlement_to_the_customer() -> None:
    posting = deposit_posting(account_id=A, settlement_account_id=S, amount_minor=500)

    assert posting.kind is TransactionKind.DEPOSIT
    assert set(posting.lines) == {PostingLine(S, -500), PostingLine(A, 500)}


def test_withdrawal_moves_money_from_the_customer_to_settlement() -> None:
    posting = withdrawal_posting(account_id=A, settlement_account_id=S, amount_minor=500)

    assert posting.kind is TransactionKind.WITHDRAWAL
    assert set(posting.lines) == {PostingLine(A, -500), PostingLine(S, 500)}


def test_transfer_debits_the_source_and_credits_the_destination() -> None:
    posting = transfer_posting(source_account_id=A, destination_account_id=B, amount_minor=500)

    assert posting.kind is TransactionKind.TRANSFER
    assert set(posting.lines) == {PostingLine(A, -500), PostingLine(B, 500)}


def test_transfer_to_the_same_account_is_rejected() -> None:
    with pytest.raises(SameAccountTransferError):
        transfer_posting(source_account_id=A, destination_account_id=A, amount_minor=500)


def test_builders_validate_the_amount() -> None:
    with pytest.raises(InvalidAmountError):
        deposit_posting(account_id=A, settlement_account_id=S, amount_minor=0)


def test_account_ids_are_in_lock_order() -> None:
    posting = transfer_posting(source_account_id=B, destination_account_id=A, amount_minor=1)

    assert posting.account_ids == sorted([A, B])


# --- malformed postings are programming errors -------------------------------------------------


@pytest.mark.parametrize(
    "lines",
    [
        (),
        (PostingLine(A, 0), PostingLine(B, 0)),
        (PostingLine(A, -100),),
        (PostingLine(A, -100), PostingLine(A, 100)),
        (PostingLine(A, -100), PostingLine(B, 99)),
        (PostingLine(A, -100), PostingLine(B, 100), PostingLine(S, 0)),
    ],
    ids=["empty", "zero lines", "single line", "same account twice", "unbalanced", "zero line"],
)
def test_malformed_postings_are_rejected(lines: tuple[PostingLine, ...]) -> None:
    with pytest.raises(ValueError, match=r"posting|line"):
        Posting(TransactionKind.TRANSFER, lines)


def test_postings_are_immutable() -> None:
    posting = transfer_posting(source_account_id=A, destination_account_id=B, amount_minor=1)

    with pytest.raises(AttributeError):
        posting.kind = TransactionKind.DEPOSIT  # type: ignore[misc]
