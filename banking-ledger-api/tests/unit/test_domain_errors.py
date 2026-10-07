import pytest

from ledger_api.domain.errors import ConflictError, DomainError, NotFoundError


def _concrete_errors() -> list[type[DomainError]]:
    found: list[type[DomainError]] = []
    pending = list(DomainError.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if not cls.__subclasses__():
            found.append(cls)
    return found


CONCRETE_ERRORS = _concrete_errors()


def test_there_are_concrete_domain_errors() -> None:
    assert len(CONCRETE_ERRORS) >= 4


@pytest.mark.parametrize("error_type", CONCRETE_ERRORS, ids=lambda cls: cls.__name__)
def test_every_concrete_error_has_a_code_message_and_category(
    error_type: type[DomainError],
) -> None:
    assert error_type.code
    assert error_type.message
    assert issubclass(error_type, (NotFoundError, ConflictError))


def test_error_codes_are_unique() -> None:
    codes = [error_type.code for error_type in CONCRETE_ERRORS]

    assert len(codes) == len(set(codes))


def test_error_carries_its_message() -> None:
    error_type = CONCRETE_ERRORS[0]

    assert str(error_type()) == error_type.message
