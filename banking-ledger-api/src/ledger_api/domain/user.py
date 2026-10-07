from email_validator import EmailNotValidError, validate_email


class InvalidEmailError(ValueError):
    """The input is not an acceptable email address. A ValueError, so Pydantic reports it as 422."""


def normalize_email(raw: str) -> str:
    """Return the canonical form of an email address: the only form ever stored.

    MVP boundary: addresses must be ASCII. Python and PostgreSQL disagree on lowercasing some
    Unicode characters (e.g. U+0130), and Unicode allows visually identical but distinct
    addresses; supporting internationalized addresses needs a deliberate normalization policy.
    The whole address is lowercased: case-sensitive local parts are permitted by RFC 5321 but
    not used in practice, and treating Alice@ and alice@ as two users invites account
    confusion. No provider-specific rules (Gmail dots, +tags) are applied.
    """
    candidate = raw.strip()
    if not candidate.isascii():
        raise InvalidEmailError("Email address must contain only ASCII characters.")
    try:
        # No DNS lookups: validation stays pure, fast and independent of the network.
        result = validate_email(candidate, check_deliverability=False, allow_smtputf8=False)
    except EmailNotValidError as error:
        # Fixed message: email-validator's reasons quote parts of the input, and validation
        # errors must never echo what was submitted. The reason stays on __cause__.
        raise InvalidEmailError("Email address is not valid.") from error
    # ascii_email, not normalized: normalized turns an ASCII punycode domain
    # (xn--...) back into Unicode. It is None only for addresses that need SMTPUTF8, which the
    # ASCII check above already rejects; checked explicitly rather than assumed.
    if result.ascii_email is None:
        raise InvalidEmailError("Email address must contain only ASCII characters.")
    return result.ascii_email.lower()
