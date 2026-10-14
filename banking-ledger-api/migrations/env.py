import asyncio
from logging.config import fileConfig

from alembic import context
from alembic.util import CommandError
from sqlalchemy import pool, text
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

# Migrations run as ledger_owner, the owner of every object, never as a superuser (ADR 0015): a
# superuser session would resolve the names in its statements through public, where the owner
# role may create objects, and would run them with superuser privileges. Only Python code (the
# test suite, migrating its own disposable databases) can set this exemption: neither the alembic
# command line, nor -x arguments, nor any environment variable reaches config.attributes.
SUPERUSER_EXEMPTION = "allow_superuser_migrations"

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


def refuse_superuser(connection: Connection) -> None:
    """Fail closed before any migration if the session or current user is a superuser."""
    if config.attributes.get(SUPERUSER_EXEMPTION) is True:
        return
    superuser = connection.execute(
        text(
            "SELECT pg_catalog.bool_or(r.rolsuper) FROM pg_catalog.pg_roles r "
            "WHERE r.rolname IN (current_user, session_user)"
        )
    ).scalar()
    if superuser is not False:  # NULL (no such role) refuses too
        raise CommandError(
            "refusing to run migrations as a PostgreSQL superuser: run them as ledger_owner "
            "(DATABASE_URL with the owner's credentials), after the role bootstrap "
            "(python -m ledger_api.admin.bootstrap)"
        )


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Deliberately not the application's engine: no pool and no statement_timeout, because a
    # migration may legitimately run longer than any request.
    # hide_parameters: a failed migration's error message shows its SQL, never bound values.
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool, hide_parameters=True)
    async with engine.connect() as connection:
        await connection.run_sync(refuse_superuser)
        # End the check's transaction: Alembic must begin (and commit) its own.
        await connection.commit()
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif shared_connection is not None:
    refuse_superuser(shared_connection)
    do_run_migrations(shared_connection)
else:
    asyncio.run(run_async_migrations())
