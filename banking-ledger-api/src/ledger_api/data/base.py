import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from sqlalchemy import BigInteger, DateTime, Dialect, MetaData, Text, func, text
from sqlalchemy.orm import DeclarativeBase, mapped_column
from sqlalchemy.types import TypeDecorator

# Deterministic constraint names, so migrations can reference them and tests can assert on them.
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# UUIDv7 is time-ordered (B-tree friendly) and not enumerable. Python generates it so the domain
# can build a posting before any I/O; the server default covers rows inserted by raw SQL.
UUIDPrimaryKey = Annotated[
    uuid.UUID,
    mapped_column(primary_key=True, default=uuid.uuid7, server_default=text("uuidv7()")),
]

CreatedAt = Annotated[datetime, mapped_column(DateTime(timezone=True), server_default=func.now())]

# A bare Mapped[int] maps to 32-bit INTEGER (max ~£21M in pence). Money is always BIGINT.
MinorUnits = Annotated[int, mapped_column(BigInteger)]


class TextEnum[E: StrEnum](TypeDecorator[E]):
    """Stores a StrEnum as TEXT. The table's CHECK constraint stays the authority on values."""

    impl = Text
    cache_ok = True

    def __init__(self, enum_type: type[E]) -> None:
        super().__init__()
        self.enum_type = enum_type  # attribute name matches the argument: used for the cache key

    def process_bind_param(self, value: E | None, dialect: Dialect) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: Any | None, dialect: Dialect) -> E | None:
        return None if value is None else self.enum_type(value)
