from enum import StrEnum


class AccountKind(StrEnum):
    # A user's account. Its balance can never go below zero.
    CUSTOMER = "customer"
    # The per-ledger external settlement account. It mirrors customer money and may go negative.
    SYSTEM = "system"
