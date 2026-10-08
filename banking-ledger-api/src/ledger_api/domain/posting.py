"""Double-entry posting rules. Pure: no I/O.

A Posting is the complete, balanced set of lines one transaction will write. Building one is
the first filter for malformed postings; the database's deferred triggers remain the
authority (ADR 0003, ADR 0010).
"""

import uuid
from dataclasses import dataclass

from ledger_api.domain.errors import InvalidAmountError, SameAccountTransferError
from ledger_api.domain.transaction import TransactionKind

# Per-transaction limit (ADR 0002): 1,000,000.00 in either MVP currency. Also keeps every
# balance far from the BIGINT range.
MAX_AMOUNT_MINOR = 100_000_000


def validate_amount(amount_minor: int) -> int:
    """A positive whole number of minor units, at most MAX_AMOUNT_MINOR."""
    # type() rather than isinstance(): bool is a subclass of int, and True is not an amount.
    if type(amount_minor) is not int or not 0 < amount_minor <= MAX_AMOUNT_MINOR:
        raise InvalidAmountError
    return amount_minor


@dataclass(frozen=True, slots=True)
class PostingLine:
    account_id: uuid.UUID
    # Signed: negative debits the account's balance, positive credits it.
    amount_minor: int


@dataclass(frozen=True, slots=True)
class Posting:
    kind: TransactionKind
    lines: tuple[PostingLine, ...]

    def __post_init__(self) -> None:
        # A malformed posting is a programming error, not a business rejection: ValueError.
        if len(self.lines) < 2:
            raise ValueError("a posting needs at least two lines")
        if len({line.account_id for line in self.lines}) != len(self.lines):
            raise ValueError("an account may appear only once in a posting")
        if any(type(line.amount_minor) is not int or line.amount_minor == 0 for line in self.lines):
            raise ValueError("every line needs a non-zero integer amount")
        if sum(line.amount_minor for line in self.lines) != 0:
            raise ValueError("a posting's lines must sum to zero")

    @property
    def account_ids(self) -> list[uuid.UUID]:
        """Every account the posting touches, in lock order (ascending id, ADR 0010)."""
        return sorted(line.account_id for line in self.lines)


def deposit_posting(
    *, account_id: uuid.UUID, settlement_account_id: uuid.UUID, amount_minor: int
) -> Posting:
    """Money arriving from outside: the settlement account goes down, the customer's up."""
    amount = validate_amount(amount_minor)
    return Posting(
        TransactionKind.DEPOSIT,
        (PostingLine(settlement_account_id, -amount), PostingLine(account_id, amount)),
    )


def withdrawal_posting(
    *, account_id: uuid.UUID, settlement_account_id: uuid.UUID, amount_minor: int
) -> Posting:
    """Money leaving to outside: the customer's balance goes down, the settlement account's up."""
    amount = validate_amount(amount_minor)
    return Posting(
        TransactionKind.WITHDRAWAL,
        (PostingLine(account_id, -amount), PostingLine(settlement_account_id, amount)),
    )


def transfer_posting(
    *, source_account_id: uuid.UUID, destination_account_id: uuid.UUID, amount_minor: int
) -> Posting:
    amount = validate_amount(amount_minor)
    if source_account_id == destination_account_id:
        raise SameAccountTransferError
    return Posting(
        TransactionKind.TRANSFER,
        (PostingLine(source_account_id, -amount), PostingLine(destination_account_id, amount)),
    )
