import pytest

from ledger_api.domain.user import InvalidEmailError, normalize_email


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("alice@example.com", "alice@example.com"),
        ("Alice@Example.COM", "alice@example.com"),
        ("  bob@example.com\t\n", "bob@example.com"),
        ("first.last+tag@sub.example.com", "first.last+tag@sub.example.com"),
    ],
)
def test_normalize_email_trims_and_lowercases(raw: str, canonical: str) -> None:
    assert normalize_email(raw) == canonical


def test_normalize_email_is_idempotent() -> None:
    canonical = normalize_email("Alice@Example.COM")

    assert normalize_email(canonical) == canonical


def test_provider_specific_rules_are_not_applied() -> None:
    # Dots and +tags are meaningful for most providers; only Gmail ignores them.
    assert normalize_email("first.last+news@gmail.com") == "first.last+news@gmail.com"


@pytest.mark.parametrize(
    "raw",
    [
        "ålice@example.com",  # non-ASCII local part
        "alice@exämple.com",  # non-ASCII (internationalized) domain
        "İnfo@example.com",  # U+0130: Python and PostgreSQL lowercase it differently
    ],
)
def test_non_ascii_addresses_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidEmailError, match="ASCII"):
        normalize_email(raw)


def test_punycode_domain_stays_ascii() -> None:
    # email-validator's "normalized" form would turn this back into Unicode (exämple.com).
    assert normalize_email("a@XN--EXMPLE-CUA.com") == "a@xn--exmple-cua.com"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "alice",
        "alice@",
        "@example.com",
        "alice@localhost",
        "a..b@example.com",
        "Alice <alice@example.com>",
        '"quoted"@example.com',
        "alice@[127.0.0.1]",
        "alice@example.test",  # special-use domain
        "x" * 250 + "@example.com",  # longer than 254 characters
    ],
)
def test_invalid_addresses_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidEmailError):
        normalize_email(raw)


def test_invalid_email_error_is_a_value_error() -> None:
    # Pydantic turns ValueError raised in a validator into a 422 validation error.
    assert issubclass(InvalidEmailError, ValueError)
