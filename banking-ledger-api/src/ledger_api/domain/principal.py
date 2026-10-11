"""The authenticated caller (ADR 0013). A plain value: never an ORM object or anything bound to
a database session, so it can cross every layer after the authentication transaction ended."""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    session_id: uuid.UUID
