"""SCRAM-SHA-256 password verifiers, computed client-side (RFC 5802, RFC 7677). Pure, no I/O.

PostgreSQL stores a role's password as a verifier, never as the password:

    SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>    (base64 fields)

and ALTER ROLE ... PASSWORD accepts such a verifier as is. Computing it here, as libpq's
PQencryptPasswordConn() and psql's \\password do, keeps the plaintext out of every SQL text,
where neither a bind parameter nor hide_parameters could hide it: a utility statement takes no
parameters, and its text reaches statement logs, pg_stat_activity and an error's [SQL: ...].

A verifier is still sensitive (it allows offline guessing): it is never logged either.
"""

import base64
import hashlib
import hmac
import re
import secrets

# PostgreSQL's defaults (scram_iterations, SCRAM_DEFAULT_SALT_LEN).
DEFAULT_ITERATIONS = 4096
SALT_BYTES = 16

# Visible ASCII, 16-256 characters. PostgreSQL prepares SCRAM passwords with SASLprep
# (RFC 4013), which leaves such a string unchanged, so the verifier computed here is exactly
# the one the server derives at login. No spaces: an environment value with a leading or
# trailing space is too easy to get wrong.
ROLE_PASSWORD_PATTERN = re.compile(r"[\x21-\x7e]{16,256}")


class InvalidRolePasswordError(ValueError):
    """A role password outside the accepted alphabet or length. The message never quotes it."""

    def __init__(self) -> None:
        super().__init__("a role password must be 16-256 visible ASCII characters")


def check_role_password(password: str) -> None:
    if not ROLE_PASSWORD_PATTERN.fullmatch(password):
        raise InvalidRolePasswordError


def scram_sha256_verifier(
    password: str, *, salt: bytes | None = None, iterations: int = DEFAULT_ITERATIONS
) -> str:
    """The verifier PostgreSQL would store for `password`, with a random salt by default."""
    check_role_password(password)
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    if not salt:
        raise ValueError("a SCRAM salt must not be empty")
    if iterations < 1:
        raise ValueError("SCRAM iterations must be positive")

    salted_password = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, iterations)
    client_key = hmac.digest(salted_password, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted_password, b"Server Key", "sha256")
    return f"SCRAM-SHA-256${iterations}:{_b64(salt)}${_b64(stored_key)}:{_b64(server_key)}"


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
