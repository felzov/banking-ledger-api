"""Business-rule failures. Pure: the API layer decides how each category maps to HTTP."""

from typing import ClassVar


class DomainError(Exception):
    # Stable, machine-readable identifier exposed to API clients.
    code: ClassVar[str]
    # Safe, human-readable message. Never includes identifiers or internal details.
    message: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(self.message)


class NotFoundError(DomainError):
    """The resource does not exist, or exists but is not visible to the caller."""


class ConflictError(DomainError):
    """The request conflicts with existing state (a uniqueness rule)."""


class RuleViolationError(DomainError):
    """A well-formed request that a business rule rejects (e.g. insufficient funds)."""


class UnavailableError(DomainError):
    """A transient condition (contention): nothing happened, and retrying may succeed."""


class UserNotFoundError(NotFoundError):
    code = "user_not_found"
    message = "User not found."


class AccountNotFoundError(NotFoundError):
    # Deliberately also raised for another user's account and for system accounts, so the
    # response cannot be used to discover which account IDs exist (ADR 0009).
    code = "account_not_found"
    message = "Account not found."


class EmailAlreadyRegisteredError(ConflictError):
    code = "email_already_registered"
    message = "A user with this email address already exists."


class AccountAlreadyExistsError(ConflictError):
    code = "account_already_exists"
    message = "The user already has an account in this currency."


class DestinationAccountNotFoundError(NotFoundError):
    # A transfer's destination: missing, or not a customer account. Revealing that a
    # destination exists is inherent to paying into it; its owner and balance stay private.
    code = "destination_account_not_found"
    message = "Destination account not found."


class InvalidAmountError(RuleViolationError):
    code = "invalid_amount"
    message = (
        "Amount must be a positive whole number of minor units within the per-transaction limit."
    )


class SameAccountTransferError(RuleViolationError):
    code = "same_account_transfer"
    message = "Source and destination accounts must differ."


class CurrencyMismatchError(RuleViolationError):
    code = "currency_mismatch"
    message = "Accounts in different currencies cannot transact with each other."


class InsufficientFundsError(RuleViolationError):
    code = "insufficient_funds"
    message = "The account balance is insufficient."


class TemporarilyUnavailableError(UnavailableError):
    # Lock timeout, statement timeout or exhausted deadlock retries. Nothing was written.
    code = "temporarily_unavailable"
    message = "The service is temporarily unavailable. Please retry."
