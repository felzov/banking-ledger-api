import contextlib
import logging
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine.interfaces import AdaptedConnection, DBAPIConnection, Dialect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

from ledger_api.config import Settings
from ledger_api.data.errors import DatabaseUnavailableError, PrivilegedDatabaseRoleError

logger = logging.getLogger(__name__)

# Per-connection PostgreSQL limits (ADR 0005): a stuck lock or runaway query fails fast instead
# of holding a pooled connection, and a transaction abandoned mid-way cannot hold locks forever.
LOCK_TIMEOUT_MS = 2_000
STATEMENT_TIMEOUT_MS = 5_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 10_000


def create_engine(settings: Settings, *, privilege_guard: bool = True) -> AsyncEngine:
    """Create the application's engine. No connection is opened until first use.

    privilege_guard: every new connection is refused unless its role is an unprivileged one
    (refuse_privileged_role). Disabling it is a Python-only hook for tests that deliberately
    connect as a superuser; no setting or environment variable reaches it.
    """
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        # Detects connections dropped by a database restart before handing them out.
        pool_pre_ping=True,
        # Bound values (password hashes, token digests, emails) never appear in a database
        # error's message, which ends up in tracebacks and the audit fallback log. The SQL stays.
        hide_parameters=True,
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
    if privilege_guard:
        event.listen(engine.sync_engine, "connect", refuse_privileged_role)
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


# Predefined roles that grant far more than the application needs: reading or writing every table,
# the server's files and programs, other sessions, maintenance. Membership in any of them, or in
# a role with a dangerous attribute or owning an object, makes the connection privileged.
DANGEROUS_PREDEFINED_ROLES = (
    "pg_read_all_data",
    "pg_write_all_data",
    "pg_read_server_files",
    "pg_write_server_files",
    "pg_execute_server_program",
    "pg_signal_backend",
    "pg_signal_autovacuum_worker",
    "pg_maintain",
    "pg_checkpoint",
    "pg_create_subscription",
)

# The effective identity of the connection, read from the server: both session_user (who logged
# in) and current_user (who acts). pg_has_role(..., 'MEMBER') follows memberships transitively,
# whether they INHERIT or only allow SET ROLE: either way the role can act with the other's
# power. Run with search_path pinned to pg_catalog (no object in public can stand in for a
# function or operator here).
ROLE_PRIVILEGES = """
SELECT r.rolname,
       r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb, r.rolreplication,
       pg_catalog.pg_has_role(r.oid, d.datdba, 'MEMBER') AS database_owner,
       EXISTS (
           SELECT 1 FROM (
               SELECT relowner AS owner FROM pg_catalog.pg_class
                WHERE relnamespace = 'public'::pg_catalog.regnamespace
               UNION SELECT proowner FROM pg_catalog.pg_proc
                WHERE pronamespace = 'public'::pg_catalog.regnamespace
               UNION SELECT typowner FROM pg_catalog.pg_type
                WHERE typnamespace = 'public'::pg_catalog.regnamespace
           ) o
            WHERE pg_catalog.pg_has_role(r.oid, o.owner, 'MEMBER')
       ) AS object_owner,
       ARRAY(
           SELECT x.rolname::text FROM pg_catalog.pg_roles x
            WHERE x.oid <> r.oid
              AND pg_catalog.pg_has_role(r.oid, x.oid, 'MEMBER')
              AND (x.rolsuper OR x.rolbypassrls OR x.rolcreaterole OR x.rolcreatedb
                   OR x.rolreplication OR x.rolname = ANY($1::text[]))
            ORDER BY 1
       ) AS privileged_memberships
  FROM pg_catalog.pg_roles r, pg_catalog.pg_database d
 WHERE r.rolname IN (current_user, session_user)
   AND d.datname = pg_catalog.current_database()
"""

ROLE_ATTRIBUTES = (
    ("rolsuper", "is a superuser"),
    ("rolbypassrls", "bypasses row-level security"),
    ("rolcreaterole", "can create roles"),
    ("rolcreatedb", "can create databases"),
    ("rolreplication", "can replicate"),
    ("database_owner", "owns the database"),
    ("object_owner", "owns objects in schema public"),
)


async def _role_privileges(connection: Any) -> list[Any]:
    """The privilege facts of the connection's roles (asyncpg, inside the connect event)."""
    async with connection.transaction(readonly=True):
        await connection.execute("SET LOCAL search_path = pg_catalog, pg_temp")
        rows: list[Any] = await connection.fetch(ROLE_PRIVILEGES, list(DANGEROUS_PREDEFINED_ROLES))
        return rows


def refuse_privileged_role(dbapi_connection: DBAPIConnection, connection_record: Any) -> None:
    """Pool `connect` event: discard a new connection whose role is privileged (ADR 0015).

    The application connects as ledger_app, which owns nothing and holds only migration 0013's
    grants. Anything more means a misconfiguration (the admin or the owner URL given to the API,
    a role granted too much), and serving with it would let a bug or an attacker disable the
    ledger's triggers or read the audit trail. Checked on the server, on every new physical
    connection (reused pooled connections were checked when they were opened); a check that
    cannot complete refuses too (fail closed).

    The reasons go to a CRITICAL log line, with the role's name but never a URL or password.
    """
    try:
        if not isinstance(dbapi_connection, AdaptedConnection):
            raise TypeError("the privilege check needs an asyncio driver connection")
        rows = dbapi_connection.run_async(_role_privileges)
        problems = sorted(
            {
                f"{row['rolname']} {meaning}"
                for row in rows
                for attribute, meaning in ROLE_ATTRIBUTES
                if row[attribute]
            }
            | {
                f"{row['rolname']} is a member of {member}"
                for row in rows
                for member in row["privileged_memberships"]
            }
        )
        if not rows:
            problems = ["the connection's role was not found"]
    except Exception as error:
        _discard(dbapi_connection)
        logger.critical(
            "refusing the database connection: its role's privileges could not be verified (%s)",
            type(error).__name__,
        )
        raise PrivilegedDatabaseRoleError("could not verify the database role") from None
    if problems:
        _discard(dbapi_connection)
        logger.critical(
            "refusing the database connection: its role is privileged: %s", "; ".join(problems)
        )
        raise PrivilegedDatabaseRoleError("the database role is privileged")


def _discard(dbapi_connection: DBAPIConnection) -> None:
    # Best effort: the refusal, not a failure to close, is what the caller must see.
    with contextlib.suppress(Exception):
        dbapi_connection.close()


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: with the default, reading an attribute after commit triggers an
    # implicit refresh query, which async SQLAlchemy cannot do and raises instead.
    return async_sessionmaker(engine, expire_on_commit=False)
