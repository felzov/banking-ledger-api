from typing import Any

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection, Dialect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

from ledger_api.config import Settings
from ledger_api.data.errors import DatabaseUnavailableError

# Per-connection PostgreSQL limits (ADR 0005): a stuck lock or runaway query fails fast instead
# of holding a pooled connection, and a transaction abandoned mid-way cannot hold locks forever.
LOCK_TIMEOUT_MS = 2_000
STATEMENT_TIMEOUT_MS = 5_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 10_000


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the application's engine. No connection is opened until first use."""
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        # Detects connections dropped by a database restart before handing them out.
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": "banking-ledger-api",
                "lock_timeout": str(LOCK_TIMEOUT_MS),
                "statement_timeout": str(STATEMENT_TIMEOUT_MS),
                "idle_in_transaction_session_timeout": str(IDLE_IN_TRANSACTION_TIMEOUT_MS),
            }
        },
    )
    event.listen(engine.sync_engine, "do_connect", _connect_or_unavailable)
    return engine


def _connect_or_unavailable(
    dialect: Dialect,
    connection_record: ConnectionPoolEntry,
    cargs: list[Any],
    cparams: dict[str, Any],
) -> DBAPIConnection:
    """Open a DBAPI connection exactly as SQLAlchemy would, but report an OSError raised while
    connecting (refused, unreachable, timed out) as DatabaseUnavailableError.

    The driver raises such errors raw, and SQLAlchemy's handle_error event never sees them, so
    this is the one place that knows an OSError came from establishing a connection. Every new
    connection passes through here: the first, and every replacement after a failed pre-ping.
    The message names no host, user or other connection parameter.
    """
    try:
        return dialect.connect(*cargs, **cparams)
    except OSError as error:
        raise DatabaseUnavailableError("could not connect to the database") from error


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: with the default, reading an attribute after commit triggers an
    # implicit refresh query, which async SQLAlchemy cannot do and raises instead.
    return async_sessionmaker(engine, expire_on_commit=False)
