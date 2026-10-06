from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ledger_api.config import Settings

# Per-connection PostgreSQL limits (ADR 0005): a stuck lock or runaway query fails fast instead
# of holding a pooled connection, and a transaction abandoned mid-way cannot hold locks forever.
LOCK_TIMEOUT_MS = 2_000
STATEMENT_TIMEOUT_MS = 5_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 10_000


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the application's engine. No connection is opened until first use."""
    return create_async_engine(
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


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: with the default, reading an attribute after commit triggers an
    # implicit refresh query, which async SQLAlchemy cannot do and raises instead.
    return async_sessionmaker(engine, expire_on_commit=False)
