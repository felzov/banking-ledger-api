"""Opaque bearer tokens (Phase 8, ADR 0013). Pure computation, no I/O.

A token is 32 random bytes from the OS CSPRNG (256 bits), base64url-encoded, behind a fixed
prefix so secret scanners can recognise a leaked one. The plaintext is returned to the client
exactly once, at login. Only its SHA-256 digest is stored: a fast hash is enough because the
input is uniformly random and has 256 bits of entropy (there is nothing to brute-force, unlike
a password), so a database dump yields no usable token. No signing key exists.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass, field

# The public, fixed prefix of every token (not a secret; it identifies one when leaked).
TOKEN_PREFIX = "lt_"  # noqa: S105
TOKEN_BYTES = 32
# base64url without padding: 4 characters per 3 bytes, rounded up.
TOKEN_BODY_LENGTH = 43
TOKEN_PATTERN = re.compile(rf"{TOKEN_PREFIX}[A-Za-z0-9_-]{{{TOKEN_BODY_LENGTH}}}")
TOKEN_HASH_BYTES = 32  # SHA-256


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A newly generated token and the digest to store. Neither appears in repr()."""

    token: str = field(repr=False)
    token_hash: bytes = field(repr=False)


def generate_token() -> IssuedToken:
    token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"
    return IssuedToken(token=token, token_hash=hash_token(token))


def hash_token(token: str) -> bytes:
    """The SHA-256 digest of the whole token (prefix included): what is stored and looked up.

    UTF-8, so even a malformed client-supplied value hashes instead of raising (callers check
    is_well_formed_token first anyway).
    """
    return hashlib.sha256(token.encode("utf-8")).digest()


def is_well_formed_token(candidate: object) -> bool:
    """A cheap shape check, so obviously malformed input is refused before any lookup. Says
    nothing about whether the token exists, is unexpired or unrevoked."""
    return isinstance(candidate, str) and TOKEN_PATTERN.fullmatch(candidate) is not None
