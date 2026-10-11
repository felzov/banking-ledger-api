"""Opaque bearer tokens (ADR 0013). Pure: no I/O."""

import base64
import hashlib
import logging

import pytest

from ledger_api.domain.tokens import (
    TOKEN_BODY_LENGTH,
    TOKEN_BYTES,
    TOKEN_HASH_BYTES,
    TOKEN_PREFIX,
    generate_token,
    hash_token,
    is_well_formed_token,
)


def _body(token: str) -> str:
    assert token.startswith(TOKEN_PREFIX)
    return token[len(TOKEN_PREFIX) :]


def test_a_token_is_the_prefix_and_43_base64url_characters() -> None:
    issued = generate_token()

    assert issued.token.startswith(TOKEN_PREFIX)
    assert len(_body(issued.token)) == TOKEN_BODY_LENGTH
    assert is_well_formed_token(issued.token)


def test_a_token_carries_256_random_bits() -> None:
    body = _body(generate_token().token)

    raw = base64.urlsafe_b64decode(body + "=")  # one padding character for 43 characters

    assert len(raw) == TOKEN_BYTES == 32


def test_tokens_do_not_repeat() -> None:
    tokens = {generate_token().token for _ in range(2_000)}

    assert len(tokens) == 2_000


def test_tokens_come_from_the_os_csprng(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def recording_token_urlsafe(nbytes: int) -> str:
        calls.append(nbytes)
        return "A" * TOKEN_BODY_LENGTH

    monkeypatch.setattr("ledger_api.domain.tokens.secrets.token_urlsafe", recording_token_urlsafe)

    issued = generate_token()

    assert calls == [TOKEN_BYTES]
    assert issued.token == TOKEN_PREFIX + "A" * TOKEN_BODY_LENGTH


def test_the_stored_hash_is_the_sha256_of_the_whole_token() -> None:
    issued = generate_token()

    assert issued.token_hash == hashlib.sha256(issued.token.encode()).digest()
    assert issued.token_hash == hash_token(issued.token)
    assert len(issued.token_hash) == TOKEN_HASH_BYTES


def test_hashing_is_deterministic_and_distinguishes_tokens() -> None:
    first, second = generate_token(), generate_token()

    assert hash_token(first.token) == hash_token(first.token)
    assert hash_token(first.token) != hash_token(second.token)
    assert first.token.encode() not in hash_token(first.token)


def test_hashing_never_raises_on_client_supplied_text() -> None:
    assert len(hash_token("lt_\N{EURO SIGN}not ascii at all")) == TOKEN_HASH_BYTES
    assert len(hash_token("")) == TOKEN_HASH_BYTES


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "A" * TOKEN_BODY_LENGTH,  # no prefix
        "LT_" + "A" * TOKEN_BODY_LENGTH,  # wrong-case prefix
        TOKEN_PREFIX + "A" * (TOKEN_BODY_LENGTH - 1),  # too short
        TOKEN_PREFIX + "A" * (TOKEN_BODY_LENGTH + 1),  # too long
        TOKEN_PREFIX + "A" * (TOKEN_BODY_LENGTH - 1) + "=",  # padding
        TOKEN_PREFIX + "A" * (TOKEN_BODY_LENGTH - 1) + "+",  # standard base64, not base64url
        " " + TOKEN_PREFIX + "A" * TOKEN_BODY_LENGTH,  # whitespace
        TOKEN_PREFIX + "A" * TOKEN_BODY_LENGTH + "\n",  # trailing newline
        None,
        b"lt_" + b"A" * TOKEN_BODY_LENGTH,
    ],
)
def test_malformed_tokens_are_recognised(candidate: object) -> None:
    assert not is_well_formed_token(candidate)


def test_an_issued_token_never_appears_in_repr_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    issued = generate_token()

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").info("issued %r", issued)

    for text in (repr(issued), str(issued), caplog.text):
        assert issued.token not in text
        assert _body(issued.token) not in text
        assert issued.token_hash.hex() not in text
        assert repr(issued.token_hash) not in text
