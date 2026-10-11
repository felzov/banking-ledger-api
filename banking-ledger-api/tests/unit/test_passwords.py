"""Password policy and Argon2id hashing (ADR 0013). Pure: no I/O.

Hashing uses deliberately cheap parameters here (the policy and the API are what is tested,
not Argon2's cost). Production parameters are covered by the configuration tests.
"""

import logging

import argon2.profiles
import pytest
from argon2 import PasswordHasher, Type
from hypothesis import given, settings
from hypothesis import strategies as st

from ledger_api.domain.passwords import (
    ARGON2ID_PREFIX,
    DEFAULT_PARAMETERS,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    Argon2Parameters,
    InvalidPasswordError,
    MalformedPasswordHashError,
    hash_password,
    make_hasher,
    needs_rehash,
    normalize_password,
    verify_password,
)

CHEAP = Argon2Parameters(time_cost=1, memory_cost=8, parallelism=1)
PHRASE = "correct horse battery staple"


@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    return make_hasher(CHEAP)


# --- hashing and verification -------------------------------------------------------------------


def test_a_correct_password_verifies_and_a_wrong_one_does_not(hasher: PasswordHasher) -> None:
    stored = hash_password(hasher, PHRASE)

    assert verify_password(hasher, stored, PHRASE)
    assert not verify_password(hasher, stored, PHRASE + "!")
    assert not verify_password(hasher, stored, PHRASE.upper())


def test_the_hash_is_argon2id_with_the_configured_parameters(hasher: PasswordHasher) -> None:
    stored = hash_password(hasher, PHRASE)

    assert stored.startswith(ARGON2ID_PREFIX)
    assert "$m=8,t=1,p=1$" in stored
    assert PHRASE not in stored


def test_each_hash_has_its_own_salt(hasher: PasswordHasher) -> None:
    first, second = hash_password(hasher, PHRASE), hash_password(hasher, PHRASE)

    assert first != second
    assert verify_password(hasher, first, PHRASE)
    assert verify_password(hasher, second, PHRASE)


def test_the_hasher_always_produces_argon2id() -> None:
    assert make_hasher(CHEAP).type is Type.ID


def test_the_default_parameters_are_rfc_9106_low_memory() -> None:
    profile = argon2.profiles.RFC_9106_LOW_MEMORY

    assert (
        DEFAULT_PARAMETERS.time_cost,
        DEFAULT_PARAMETERS.memory_cost,
        DEFAULT_PARAMETERS.parallelism,
        DEFAULT_PARAMETERS.hash_len,
        DEFAULT_PARAMETERS.salt_len,
    ) == (
        profile.time_cost,
        profile.memory_cost,
        profile.parallelism,
        profile.hash_len,
        profile.salt_len,
    )


# --- NFKC normalization -------------------------------------------------------------------------


def _full_width(text: str) -> str:
    """The full-width form of ASCII text (U+FF01-U+FF5E), built here so the file holds no
    look-alike characters."""
    return "".join(chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in text)


@pytest.mark.parametrize(
    ("registered", "typed"),
    [
        (_full_width("secret passphrase"), "secret passphrase"),
        ("\N{LATIN SMALL LIGATURE FI}ve little words", "five little words"),
        (
            "caf\N{LATIN SMALL LETTER E WITH ACUTE} au lait please",
            "cafe\N{COMBINING ACUTE ACCENT} au lait please",
        ),
    ],
    ids=["full-width", "ligature", "composed-vs-decomposed"],
)
def test_compatibility_variants_of_a_password_are_the_same_password(
    hasher: PasswordHasher, registered: str, typed: str
) -> None:
    # Both directions: normalization must happen at registration AND at verification.
    assert verify_password(hasher, hash_password(hasher, registered), typed)
    assert verify_password(hasher, hash_password(hasher, typed), registered)


def test_normalization_is_nfkc_and_idempotent() -> None:
    normalized = normalize_password("\N{LATIN SMALL LIGATURE FI}ve little words")

    assert normalized == "five little words"
    assert normalize_password(normalized) == normalized


# --- the length policy --------------------------------------------------------------------------


@pytest.mark.parametrize("length", [MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH])
def test_the_length_bounds_are_accepted(length: int) -> None:
    assert len(normalize_password("p" * length)) == length


@pytest.mark.parametrize("length", [0, 1, MIN_PASSWORD_LENGTH - 1, MAX_PASSWORD_LENGTH + 1, 10_000])
def test_lengths_outside_the_policy_are_rejected(hasher: PasswordHasher, length: int) -> None:
    with pytest.raises(InvalidPasswordError):
        normalize_password("p" * length)
    with pytest.raises(InvalidPasswordError):
        hash_password(hasher, "p" * length)


def test_length_is_counted_after_normalization() -> None:
    # 12 code points as typed, 11 after NFKC (e + combining acute become one é): too short.
    decomposed = "abcdefghij" + "e\N{COMBINING ACUTE ACCENT}"
    assert len(decomposed) == MIN_PASSWORD_LENGTH
    with pytest.raises(InvalidPasswordError):
        normalize_password(decomposed)

    # 10 code points as typed, 12 after NFKC (the ligature becomes three letters): long enough.
    ligature = "abcdefghi\N{LATIN SMALL LIGATURE FFI}"
    assert len(ligature) == MIN_PASSWORD_LENGTH - 2
    assert len(normalize_password(ligature)) == MIN_PASSWORD_LENGTH


def test_there_are_no_composition_rules() -> None:
    for password in ("aaaaaaaaaaaa", "123456789012", "            ", "pass phrase only"):
        assert normalize_password(password) == password


@pytest.mark.parametrize("value", [None, 123456789012, b"bytes password", ["list"]])
def test_non_strings_are_rejected(value: object) -> None:
    with pytest.raises(InvalidPasswordError):
        normalize_password(value)  # type: ignore[arg-type]


def test_verification_does_not_apply_the_length_policy(hasher: PasswordHasher) -> None:
    # A stored hash always comes from a compliant password: a non-compliant input does not
    # match, and verifying it is not an error.
    stored = hash_password(hasher, PHRASE)

    assert not verify_password(hasher, stored, "short")
    assert not verify_password(hasher, stored, "x" * (MAX_PASSWORD_LENGTH * 4))


@settings(max_examples=25, deadline=None)
@given(st.text(min_size=MIN_PASSWORD_LENGTH, max_size=40))
def test_any_policy_compliant_password_round_trips(raw: str) -> None:
    try:
        normalize_password(raw)
    except InvalidPasswordError:
        return  # NFKC moved it outside the bounds
    hasher = make_hasher(CHEAP)

    assert verify_password(hasher, hash_password(hasher, raw), raw)


# --- malformed stored hashes and rehashing ------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "not-a-hash",
        "$argon2id$garbage",
        "$argon2i$v=19$m=8,t=1,p=1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo",  # another Argon2 type
        "$2b$12$abcdefghijklmnopqrstuuabcdefghijklmnopqrstuvwxyz012",  # bcrypt
    ],
    ids=["empty", "garbage", "truncated-argon2id", "argon2i", "bcrypt"],
)
def test_a_malformed_or_foreign_hash_fails_closed(hasher: PasswordHasher, stored: str) -> None:
    with pytest.raises(MalformedPasswordHashError) as verify_error:
        verify_password(hasher, stored, PHRASE)
    with pytest.raises(MalformedPasswordHashError) as rehash_error:
        needs_rehash(hasher, stored)

    for error in (verify_error.value, rehash_error.value):
        if stored:
            assert stored not in str(error)


def test_a_hash_made_with_weaker_parameters_needs_rehashing(hasher: PasswordHasher) -> None:
    stored = hash_password(hasher, PHRASE)
    stronger = make_hasher(Argon2Parameters(time_cost=2, memory_cost=16, parallelism=1))

    assert needs_rehash(stronger, stored)
    assert not needs_rehash(hasher, stored)
    assert not needs_rehash(stronger, hash_password(stronger, PHRASE))


# --- no secret in errors or logs ----------------------------------------------------------------


def test_policy_errors_never_contain_the_password(caplog: pytest.LogCaptureFixture) -> None:
    too_short = "tiny-phrase"  # too short on purpose

    with pytest.raises(InvalidPasswordError) as excinfo:
        normalize_password(too_short)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").exception("registration refused", exc_info=excinfo.value)

    assert too_short not in str(excinfo.value)
    assert too_short not in repr(excinfo.value)
    assert str(len(too_short)) not in str(excinfo.value)  # not even its length
    assert too_short not in caplog.text


def test_verification_errors_never_contain_the_password_or_the_hash(
    hasher: PasswordHasher, caplog: pytest.LogCaptureFixture
) -> None:
    stored = hash_password(hasher, PHRASE)
    corrupted = stored[:-4] + "!!!!"

    with pytest.raises(MalformedPasswordHashError) as excinfo:
        verify_password(hasher, corrupted, PHRASE)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").exception("login failed", exc_info=excinfo.value)

    for text in (str(excinfo.value), repr(excinfo.value), caplog.text):
        assert PHRASE not in text
        assert corrupted not in text
        assert stored not in text
