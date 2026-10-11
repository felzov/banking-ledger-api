"""Password policy and hashing (Phase 8, ADR 0013). Pure computation, no I/O.

Policy (NIST SP 800-63B): 12 to 128 characters after NFKC normalization, no composition rules.
NFKC makes visually identical inputs (full-width letters, ligatures, compatibility forms) hash
to the same password, so a user can type it on any keyboard. The upper bound keeps hashing
from becoming a denial-of-service vector.

Hashing is Argon2id. It is deliberately slow and memory-hard: hash_password and
verify_password must NEVER run on the async event loop. The service layer calls them in a
worker thread (asyncio.to_thread), and never while holding a database connection.

Nothing here logs, and no error carries the password, the hash or any part of either.
"""

import unicodedata
from dataclasses import dataclass

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

# The only hash format ever stored. argon2-cffi would also verify an argon2i or argon2d hash
# (it reads the type from the hash itself), so the prefix is checked explicitly.
ARGON2ID_PREFIX = "$argon2id$"


class InvalidPasswordError(ValueError):
    """The password does not meet the policy. A ValueError, so Pydantic reports it as 422.

    The message is fixed: it never quotes the password, not even its length.
    """

    def __init__(self) -> None:
        super().__init__(
            f"Password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters long."
        )


class MalformedPasswordHashError(ValueError):
    """A stored hash is not an Argon2id hash this code can verify: a data or programming error,
    never a wrong password. Fails closed; the hash itself is not part of the message."""

    def __init__(self) -> None:
        super().__init__("Stored password hash is malformed or not Argon2id.")


@dataclass(frozen=True, slots=True)
class Argon2Parameters:
    """Argon2id cost parameters. memory_cost is in KiB."""

    time_cost: int
    memory_cost: int
    parallelism: int
    hash_len: int = 32
    salt_len: int = 16


# RFC 9106's second recommended profile ("low memory"): 3 passes, 64 MiB, 4 lanes. Above
# OWASP's minimums; the argon2-cffi default.
DEFAULT_PARAMETERS = Argon2Parameters(time_cost=3, memory_cost=65_536, parallelism=4)


def make_hasher(parameters: Argon2Parameters) -> PasswordHasher:
    return PasswordHasher(
        time_cost=parameters.time_cost,
        memory_cost=parameters.memory_cost,
        parallelism=parameters.parallelism,
        hash_len=parameters.hash_len,
        salt_len=parameters.salt_len,
        type=Type.ID,
    )


def _nfkc(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def normalize_password(raw: str) -> str:
    """The canonical form of a NEW password (registration), enforcing the policy.

    Length is counted in characters (code points) after NFKC normalization.
    """
    if not isinstance(raw, str):
        raise InvalidPasswordError
    password = _nfkc(raw)
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise InvalidPasswordError
    return password


def hash_password(hasher: PasswordHasher, raw: str) -> str:
    """Normalize, check the policy, and hash. Slow: call from a worker thread."""
    return hasher.hash(normalize_password(raw))


def verify_password(hasher: PasswordHasher, password_hash: str, raw: str) -> bool:
    """Whether `raw` is the password `password_hash` was made from. Slow: worker thread.

    The input is NFKC-normalized like at registration, but the length policy is not applied:
    a stored hash always comes from a compliant password, so a non-compliant input simply does
    not match. A wrong password is False; a malformed or non-Argon2id stored hash raises
    MalformedPasswordHashError (fails closed, and is never confused with a wrong password).
    """
    if not password_hash.startswith(ARGON2ID_PREFIX):
        raise MalformedPasswordHashError
    try:
        return hasher.verify(password_hash, _nfkc(raw))
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError) as error:
        raise MalformedPasswordHashError from error


def needs_rehash(hasher: PasswordHasher, password_hash: str) -> bool:
    """Whether a valid stored hash was made with other parameters than `hasher`'s (to be
    re-hashed after a successful login). Cheap: parses the hash, computes nothing.

    A malformed or non-Argon2id hash raises MalformedPasswordHashError rather than answering
    either way: callers only ask after a successful verification.
    """
    if not password_hash.startswith(ARGON2ID_PREFIX):
        raise MalformedPasswordHashError
    try:
        return hasher.check_needs_rehash(password_hash)
    except InvalidHashError as error:
        raise MalformedPasswordHashError from error
