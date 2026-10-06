from sqlalchemy import BigInteger, Column, Float, Numeric

from ledger_api.data.base import Base
from ledger_api.data.models import Account, LedgerEntry


def _all_columns() -> list[Column[object]]:
    return [column for table in Base.metadata.sorted_tables for column in table.columns]


def test_every_minor_unit_column_is_bigint() -> None:
    money_columns = [column for column in _all_columns() if column.name.endswith("_minor")]

    # Guards against the list silently becoming empty (e.g. a rename) and passing vacuously.
    assert {column.key for column in money_columns} == {"balance_minor", "amount_minor"}
    assert all(isinstance(column.type, BigInteger) for column in money_columns)


def test_no_column_uses_float_or_decimal_types() -> None:
    # Numeric is the base class of Float, so this rejects REAL, DOUBLE and NUMERIC alike.
    assert not [c for c in _all_columns() if isinstance(c.type, (Float, Numeric))]


def test_money_columns_are_not_nullable() -> None:
    assert Account.__table__.c.balance_minor.nullable is False
    assert LedgerEntry.__table__.c.amount_minor.nullable is False
