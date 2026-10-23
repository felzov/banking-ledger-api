"""Rate-limit policies and counter keys (ADR 0016). Pure: no I/O."""

import uuid

import pytest

from ledger_api.domain.rate_limits import (
    Limit,
    address_subject,
    counter_key,
    email_subject,
    user_subject,
)


@pytest.mark.parametrize(
    ("host", "subject"),
    [
        ("203.0.113.7", "203.0.113.7"),
        # One host usually holds a whole /64: per address, it could start afresh every time.
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),  # IPv4 through a dual-stack socket
        ("not-an-address", "unknown"),
        (None, "unknown"),
    ],
)
def test_address_subject(host: str | None, subject: str) -> None:
    assert address_subject(host) == subject


def test_an_email_is_never_in_a_key_in_clear() -> None:
    key = counter_key("ledger", Limit("login_failures", 10, 900), email_subject("a@example.com"))

    assert "a@example.com" not in key
    assert "example" not in key
    assert key == counter_key(
        "ledger", Limit("login_failures", 10, 900), email_subject("a@example.com")
    )
    assert email_subject("a@example.com") != email_subject("b@example.com")


def test_keys_are_namespaced_by_deployment_limit_and_subject() -> None:
    user = uuid.UUID("0199f6c2-7b1e-7c3a-9d4e-5f6a7b8c9d0e")

    assert counter_key("ledger", Limit("postings_user", 60, 60), user_subject(user)) == (
        "ledger:rl:postings_user:0199f6c2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
    )


@pytest.mark.parametrize(
    ("name", "maximum", "window"),
    [("Login", 1, 1), ("has space", 1, 1), ("x" * 33, 1, 1), ("ok", 0, 1), ("ok", 1, 0)],
)
def test_a_malformed_limit_is_refused(name: str, maximum: int, window: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        Limit(name, maximum, window)
