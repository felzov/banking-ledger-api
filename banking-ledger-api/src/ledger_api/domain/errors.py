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
