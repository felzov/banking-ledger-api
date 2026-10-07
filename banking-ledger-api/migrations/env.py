import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from ledger_api.config import Settings
from ledger_api.data import models  # noqa: F401  (registers every table on Base.metadata)
from ledger_api.data.base import Base

config = context.config
target_metadata = Base.metadata

# Callers such as the test suite may hand over an open connection. They own logging and the
# connection's transaction, so env.py must not reconfigure either.
shared_connection: Connection | None = config.attributes.get("connection")

if config.config_file_name is not None and shared_connection is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    return Settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    """Render the migration SQL without a database (alembic upgrade --sql), for review."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Deliberately not the application's engine: no pool and no statement_timeout, because a
    # migration may legitimately run longer than any request.
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif shared_connection is not None:
    do_run_migrations(shared_connection)
else:
    asyncio.run(run_async_migrations())
