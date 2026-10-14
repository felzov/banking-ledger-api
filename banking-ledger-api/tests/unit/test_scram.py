"""SCRAM-SHA-256 verifiers (admin.scram). Agreement with PostgreSQL's own verifier is proved
against the server in tests/integration/test_role_bootstrap.py."""

import base64
import re

import pytest

from ledger_api.admin.scram import (
    DEFAULT_ITERATIONS,
    SALT_BYTES,
    InvalidRolePasswordError,
    check_role_password,
    scram_sha256_verifier,
)

PASSWORD = "synthetic-role-password-01"  # noqa: S105 (synthetic test value)
VERIFIER = re.compile(
    r"SCRAM-SHA-256\$(?P<iterations>\d+):(?P<salt>[A-Za-z0-9+/=]+)"
    r"\$(?P<stored_key>[A-Za-z0-9+/=]+):(?P<server_key>[A-Za-z0-9+/=]+)"
)


def test_verifier_has_postgresqls_format_and_defaults() -> None:
    match = VERIFIER.fullmatch(scram_sha256_verifier(PASSWORD))

    assert match is not None
    assert int(match["iterations"]) == DEFAULT_ITERATIONS
    assert len(base64.b64decode(match["salt"])) == SALT_BYTES
    assert len(base64.b64decode(match["stored_key"])) == 32  # SHA-256
    assert len(base64.b64decode(match["server_key"])) == 32


def test_verifier_never_contains_the_password() -> None:
    assert PASSWORD not in scram_sha256_verifier(PASSWORD)


def test_same_salt_and_iterations_give_the_same_verifier() -> None:
    salt = bytes(range(16))

    assert scram_sha256_verifier(PASSWORD, salt=salt) == scram_sha256_verifier(PASSWORD, salt=salt)
    assert scram_sha256_verifier(PASSWORD, salt=salt) != scram_sha256_verifier(
        PASSWORD + "x", salt=salt
    )
    assert scram_sha256_verifier(PASSWORD, salt=salt, iterations=8192).startswith(
        "SCRAM-SHA-256$8192:"
    )


def test_salt_is_random_by_default() -> None:
    assert scram_sha256_verifier(PASSWORD) != scram_sha256_verifier(PASSWORD)


@pytest.mark.parametrize(
    "password",
    [
        "",
        "short-15-chars!",  # 15
        "x" * 257,
        "contains a space-",
        "non-ascii-pässword-1",
        "tab\tseparated-password",
    ],
)
def test_passwords_outside_the_alphabet_are_refused_without_being_quoted(password: str) -> None:
    with pytest.raises(InvalidRolePasswordError) as excinfo:
        scram_sha256_verifier(password)

    assert password == "" or password not in str(excinfo.value)


def test_boundary_lengths_are_accepted() -> None:
    check_role_password("x" * 16)
    check_role_password("~" * 256)


@pytest.mark.parametrize(("salt", "iterations"), [(b"", DEFAULT_ITERATIONS), (b"salt", 0)])
def test_degenerate_salt_or_iterations_are_refused(salt: bytes, iterations: int) -> None:
    with pytest.raises(ValueError, match="SCRAM"):
        scram_sha256_verifier(PASSWORD, salt=salt, iterations=iterations)
