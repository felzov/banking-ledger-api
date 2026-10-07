from enum import StrEnum
from types import MappingProxyType


class Currency(StrEnum):
    """Currencies supported by the MVP. Each one has exactly one ledger."""

    GBP = "GBP"
    EUR = "EUR"


# ISO 4217 minor-unit exponents: amount_minor / 10**exponent gives major units.
MINOR_UNIT_EXPONENTS = MappingProxyType({Currency.GBP: 2, Currency.EUR: 2})
