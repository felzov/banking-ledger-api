from ledger_api.domain.currency import MINOR_UNIT_EXPONENTS, Currency


def test_every_currency_has_a_minor_unit_exponent() -> None:
    assert set(MINOR_UNIT_EXPONENTS) == set(Currency)


def test_mvp_currencies_use_two_decimal_minor_units() -> None:
    assert MINOR_UNIT_EXPONENTS[Currency.GBP] == 2
    assert MINOR_UNIT_EXPONENTS[Currency.EUR] == 2
