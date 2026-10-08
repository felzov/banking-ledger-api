"""Property-based tests: posting invariants hold for every input, not just chosen examples."""

import uuid

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from ledger_api.domain.errors import InvalidAmountError
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

valid_amounts = st.integers(min_value=1, max_value=MAX_AMOUNT_MINOR)
two_distinct_ids = st.lists(st.uuids(), min_size=2, max_size=2, unique=True)
non_zero = st.integers(min_value=-(10**12), max_value=10**12).filter(lambda n: n != 0)


def _assert_well_formed(posting: Posting, amount: int) -> None:
    assert sum(line.amount_minor for line in posting.lines) == 0
    assert len({line.account_id for line in posting.lines}) == len(posting.lines) == 2
    assert sorted(abs(line.amount_minor) for line in posting.lines) == [amount, amount]


@given(ids=two_distinct_ids, amount=valid_amounts)
def test_every_builder_yields_a_balanced_two_line_posting(
    ids: list[uuid.UUID], amount: int
) -> None:
    first, second = ids
    for posting in (
        deposit_posting(account_id=first, settlement_account_id=second, amount_minor=amount),
        withdrawal_posting(account_id=first, settlement_account_id=second, amount_minor=amount),
        transfer_posting(
            source_account_id=first, destination_account_id=second, amount_minor=amount
        ),
    ):
        _assert_well_formed(posting, amount)


@given(ids=two_distinct_ids, amount=valid_amounts)
def test_customer_side_sign_matches_the_direction_of_money(
    ids: list[uuid.UUID], amount: int
) -> None:
    customer, other = ids
    deposit = deposit_posting(account_id=customer, settlement_account_id=other, amount_minor=amount)
    withdrawal = withdrawal_posting(
        account_id=customer, settlement_account_id=other, amount_minor=amount
    )

    assert PostingLine(customer, amount) in deposit.lines
    assert PostingLine(customer, -amount) in withdrawal.lines


@given(
    amount=st.one_of(
        st.integers(max_value=0),
        st.integers(min_value=MAX_AMOUNT_MINOR + 1),
        st.floats(allow_nan=True),
        st.booleans(),
    )
)
def test_out_of_range_or_non_integer_amounts_are_always_rejected(amount: object) -> None:
    with pytest.raises(InvalidAmountError):
        validate_amount(amount)  # type: ignore[arg-type]


@given(
    amounts=st.lists(non_zero, min_size=2, max_size=6).filter(lambda xs: sum(xs) != 0),
)
def test_any_unbalanced_posting_is_rejected(amounts: list[int]) -> None:
    lines = tuple(PostingLine(uuid.uuid7(), amount) for amount in amounts)

    with pytest.raises(ValueError, match="sum to zero"):
        Posting(TransactionKind.TRANSFER, lines)


@given(amounts=st.lists(non_zero, min_size=1, max_size=5))
def test_any_balanced_posting_with_distinct_accounts_is_accepted(amounts: list[int]) -> None:
    closing = -sum(amounts)
    assume(closing != 0)  # the closing line must itself be non-zero
    lines = tuple(PostingLine(uuid.uuid7(), amount) for amount in [*amounts, closing])

    posting = Posting(TransactionKind.TRANSFER, lines)

    assert sum(line.amount_minor for line in posting.lines) == 0
    assert posting.account_ids == sorted(posting.account_ids)
