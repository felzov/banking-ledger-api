"""Rate limits: how many hits a subject may make in a window, and how it is named (ADR 0016).

Pure: no I/O. A subject is who is being counted: a client address, an email address, a user.
An email address never appears in a key: only its SHA-256, so the counter store holds no
personal data in clear (ADR 0013 keeps emails out of logs and audit details for the same reason).
"""

import hashlib
import ipaddress
import re
import uuid
from dataclasses import dataclass

# A stable snake_case name: part of every counter key and of the log line of a refusal.
LIMIT_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}")


@dataclass(frozen=True, slots=True)
class Limit:
    name: str
    maximum: int
    window_seconds: int

    def __post_init__(self) -> None:
        if not LIMIT_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("a limit's name must be a short snake_case code")
        if self.maximum < 1 or self.window_seconds < 1:
            raise ValueError("a limit allows at least one hit per window of at least a second")


@dataclass(frozen=True, slots=True)
class RateLimits:
    """The API's limits, as configured (config.Settings.rate_limits)."""

    login_address: Limit
    login_failures: Limit
    registration_address: Limit
    postings_user: Limit


def address_subject(host: str | None) -> str:
    """The subject for a client address. IPv6 is counted per /64: a host usually holds a whole
    /64 and could otherwise use a fresh address for every request. An IPv4 address seen through
    a dual-stack socket counts as itself. No address (or not an IP) is one shared subject."""
    if host is None:
        return "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


def email_subject(email: str) -> str:
    """The subject for an email address, in its canonical form (domain.user.normalize_email)."""
    return "sha256-" + hashlib.sha256(email.encode()).hexdigest()


def user_subject(user_id: uuid.UUID) -> str:
    return str(user_id)


def counter_key(namespace: str, limit: Limit, subject: str) -> str:
    return f"{namespace}:rl:{limit.name}:{subject}"
