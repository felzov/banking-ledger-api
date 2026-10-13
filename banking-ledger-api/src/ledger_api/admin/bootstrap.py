"""Administrative bootstrap of the database roles (Phase 10, ADR 0015). Never run by the API.

    ADMIN_DATABASE_URL=... LEDGER_OWNER_PASSWORD=... LEDGER_APP_PASSWORD=... \\
        python -m ledger_api.admin.bootstrap

Run as a superuser, before migrations, against the application database. It separates:

- the admin (the superuser running this): nothing else ever connects as it;
- ledger_owner (LOGIN): owns every object in the public schema and runs the migrations;
- ledger_runtime (NOLOGIN group): the target of the application's grants (migration 0013);
- ledger_app (LOGIN): the API's role, a member of ledger_runtime and nothing else.

Scope. PostgreSQL roles are CLUSTER-WIDE: the three roles, their attributes, passwords and the
one membership exist for every database on the server. Everything else is confined to the ONE
database the admin URL names: its CONNECT/TEMPORARY privileges, its public schema's privileges,
and the ownership of the objects in that schema. Other databases, schemas and roles are never
changed. (Other databases keep PostgreSQL's default PUBLIC CONNECT: the login roles can connect
to them, with PUBLIC's privileges there and nothing more.)

Everything runs in ONE transaction (role DDL is transactional in PostgreSQL): a refusal or a
failure at any step leaves the cluster exactly as it was. Running it again converges to the same
state: missing roles are created, drifted attributes, passwords, membership options and database
or schema privileges of the three roles are reset. It refuses, before changing anything, to
reuse a role that is a superuser or has any membership other than ledger_app in ledger_runtime,
and to take over an object in public owned by any role other than the admin or the owner.

Ownership transfer: the official PostgreSQL image makes POSTGRES_USER the bootstrap superuser,
and REASSIGN OWNED refuses that role (it would also hand over shared objects such as the
database itself). Each object is therefore transferred explicitly with ALTER ... OWNER. A
table's indexes and identity sequences follow it (PostgreSQL refuses to move them separately).
The SECURITY DEFINER helper of migration 0007 moves with the tables, so it keeps running as the
table owner, never as the superuser; its pinned search_path and its PUBLIC EXECUTE revocation
are verified before COMMIT.

SQL: utility statements take no bind parameters, so every dynamic statement is built by
PostgreSQL's format() (%I identifiers, %L literals) from bound values, then executed as is.

Name resolution: the first statement pins search_path to pg_catalog, pg_temp (and the command's
engine sets it for the whole session). Without it, this superuser session would resolve
unqualified operators and functions through public, where the owner role may create objects:
an operator such as public.=(oid, regnamespace) is an exact match where pg_catalog's is not,
so it would run with superuser privileges (the CVE-2018-1058 class).

Passwords: only SCRAM-SHA-256 verifiers computed here (admin.scram) are sent, never a plaintext,
and a verifier only ever as a bound value: a fixed DO block builds ALTER ROLE ... PASSWORD on the
server, and replaces any failure of it with a fixed message, so the statement (verifier included)
never becomes an error's context, on the client or in the server log. Statement logging is
switched off for the transaction, and no error raised here quotes or chains a verifier.

Refusals, before or after the changes (any of them rolls everything back): a non-superuser admin,
a template database, a target role that is a superuser or has another membership, a target role
owning what it must not (the database, a schema, objects, a tablespace), default privileges that
would grant to PUBLIC, the app or the runtime group, an object in public owned by another role,
and database or schema privileges, from ANY grantor, beyond the intended ones.

Operations:
- Locks: ALTER ... OWNER takes ACCESS EXCLUSIVE locks, and the bootstrap waits at most
  lock_timeout (10 s) for any lock, its own advisory lock included. Behind live traffic, or
  behind a concurrent bootstrap that takes longer, it fails with SQLSTATE 55P03 and changes
  nothing: run it again.
- Other roles: PUBLIC loses CONNECT and TEMPORARY on the database and every privilege on the
  public schema. A non-superuser role that relied on them, e.g. a monitoring exporter, needs
  its own explicit grants afterwards (an explicit grant to a role other than the three is left
  alone).
"""

import asyncio
import re
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_api.admin.scram import InvalidRolePasswordError, scram_sha256_verifier
from ledger_api.admin.settings import BootstrapSettings
from ledger_api.data.errors import sqlstate

# Lower-case identifiers that need no quoting, within PostgreSQL's 63-byte limit. pg_ is
# reserved for PostgreSQL's predefined roles.
ROLE_NAME_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}")
RESERVED_ROLE_PREFIX = "pg_"

# GRANT ... WITH INHERIT/SET options (PostgreSQL 16).
MIN_SERVER_VERSION_NUM = 160000

# Serializes concurrent bootstraps of one database ("ledg" "er" 0001). Advisory locks are
# per-database: two bootstraps of DIFFERENT databases creating the same role at once may still
# collide, and one of them fails (and changes nothing) instead.
ADVISORY_LOCK_KEY = 0x6C65_6467_6572_0001
# ALTER ... OWNER takes an ACCESS EXCLUSIVE lock: fail rather than queue behind live traffic.
LOCK_TIMEOUT = "10s"

# The bootstrap's own name resolution: built-ins only, temporary objects last (never public).
SESSION_SEARCH_PATH = "pg_catalog, pg_temp"

# Migration 0007's SECURITY DEFINER helper and its pinned search_path (also 0008's).
SEQUENCE_HELPER = "public.ledger_entries_last_issued_sequence_number()"
ENTRIES_TABLE = "public.ledger_entries"
PINNED_SEARCH_PATH = ["search_path=pg_catalog, pg_temp"]

# Attributes every bootstrapped role has, whatever it had before.
SAFE_ATTRIBUTES = (
    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
    "CONNECTION LIMIT -1 VALID UNTIL 'infinity'"
)


class BootstrapError(Exception):
    """A refusal or failure. The message is safe to print: no password, verifier or SQL."""


@dataclass(frozen=True, slots=True)
class RoleNames:
    owner: str
    app: str
    runtime_group: str

    def __post_init__(self) -> None:
        for name in self.all():
            if not ROLE_NAME_PATTERN.fullmatch(name) or name.startswith(RESERVED_ROLE_PREFIX):
                raise ValueError(f"invalid role name {name!r}")
        if len(set(self.all())) != len(self.all()):
            raise ValueError("the owner, app and runtime group roles must be distinct")

    def all(self) -> tuple[str, str, str]:
        return (self.owner, self.app, self.runtime_group)


# The command's roles. Fixed: migration 0013 grants to "ledger_runtime" by name. Other names are
# for tests only, through bootstrap() itself.
DEFAULT_ROLE_NAMES = RoleNames(
    owner="ledger_owner", app="ledger_app", runtime_group="ledger_runtime"
)


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    database: str
    created_roles: tuple[str, ...]
    # e.g. "table public.accounts", "function public.forbid_ledger_mutation()"
    transferred_objects: tuple[str, ...]


async def bootstrap(
    connection: AsyncConnection,
    names: RoleNames,
    *,
    owner_password: SecretStr,
    app_password: SecretStr,
) -> BootstrapReport:
    """Bootstrap the database `connection` is connected to, as its (superuser) role.

    Opens and commits its own transaction, so `connection` must not be in one.
    """
    if connection.in_transaction():
        raise BootstrapError("bootstrap() runs its own transaction: pass a connection outside one")
    try:
        # Before any I/O: a bad password is refused before anything is read or changed.
        owner_verifier = scram_sha256_verifier(owner_password.get_secret_value())
        app_verifier = scram_sha256_verifier(app_password.get_secret_value())
    except InvalidRolePasswordError as error:
        raise BootstrapError(str(error)) from None

    async with connection.begin():
        await _pin_search_path(connection)  # first: before any other statement resolves a name
        database = await _check_admin(connection, names)
        await _quiet_and_serialize(connection)
        async with _step("checking existing roles"):
            await _check_existing_roles(connection, names)
            await _check_role_ownerships(connection, names)
            await _check_default_privileges(connection, names)
        created = await _reconcile_roles(connection, names, owner_verifier, app_verifier)
        await _reconcile_membership(connection, names)
        await _restrict_database(connection, names, database)
        await _restrict_schema(connection, names)
        await _check_access_control(connection, names)
        transferred = await _transfer_ownership(connection, names)
        await _check_ownership(connection, names)
        await _check_sequence_helper(connection)
    return BootstrapReport(database, created, transferred)


def create_admin_engine(url: str) -> AsyncEngine:
    """The command's engine: no pool, no bound value in any error (Phase 10 E1), and the
    pinned search_path for the whole session, so that even the driver's and the dialect's own
    setup queries never resolve a name through public."""
    return create_async_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": SESSION_SEARCH_PATH}},
    )


# --- statements --------------------------------------------------------------------------------


@asynccontextmanager
async def _step(name: str) -> AsyncIterator[None]:
    """Report a database error as a BootstrapError naming the step, chained to it. Only for
    statements that carry no secret: password statements go through _set_password()."""
    try:
        yield
    except DBAPIError as error:
        code = sqlstate(error) or "unknown"
        cause = error.orig.__cause__ if error.orig is not None else None
        message = getattr(cause, "message", None) or type(error.orig).__name__
        raise BootstrapError(f"{name} failed (SQLSTATE {code}): {message}") from error


async def _pin_search_path(connection: AsyncConnection) -> None:
    async with _step("pinning search_path"):
        await connection.execute(text(f"SET LOCAL search_path = {SESSION_SEARCH_PATH}"))


async def _execute_formatted(connection: AsyncConnection, template: str, *args: str) -> None:
    """Run a utility statement that PostgreSQL's format() builds from `template` and `args`.

    Both are bound parameters; format() quotes them (%I identifier, %L literal). The resulting
    text is executed as is (exec_driver_sql: no bind-parameter parsing of a quoted value).
    """
    statement = await connection.scalar(
        text("SELECT pg_catalog.format(:template, VARIADIC CAST(:args AS text[]))"),
        {"template": template, "args": list(args)},
    )
    if not isinstance(statement, str):  # format() returns NULL only for a NULL template
        raise BootstrapError("format() returned no statement")
    await connection.exec_driver_sql(statement)


async def _role_exists(connection: AsyncConnection, name: str) -> bool:
    found = await connection.scalar(
        text("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :name"), {"name": name}
    )
    return found is not None


# --- 1. who runs it, and what it may reuse -----------------------------------------------------


async def _check_admin(connection: AsyncConnection, names: RoleNames) -> str:
    """Read-only, before any change: a superuser, a recent enough server, not a template
    database (its public schema and privileges would be copied into every new database), not a
    target role."""
    async with _step("checking the admin role"):
        row = (
            await connection.execute(
                text(
                    "SELECT current_user, r.rolsuper, d.datname AS current_database, "
                    "d.datistemplate, current_setting('server_version_num')::int AS version "
                    "FROM pg_catalog.pg_roles r, pg_catalog.pg_database d "
                    "WHERE r.rolname = current_user AND d.datname = current_database()"
                )
            )
        ).one()
    if not row.rolsuper:
        raise BootstrapError(f"role {row.current_user} is not a superuser")
    if row.version < MIN_SERVER_VERSION_NUM:
        raise BootstrapError("PostgreSQL 16 or later is required")
    if row.datistemplate:
        raise BootstrapError(f"database {row.current_database} is a template database")
    if row.current_user in names.all():
        raise BootstrapError(
            f"role {row.current_user} runs the bootstrap and cannot be one of the roles it "
            "configures"
        )
    database: str = row.current_database
    return database


async def _quiet_and_serialize(connection: AsyncConnection) -> None:
    async with _step("preparing the transaction"):
        # Password statements carry verifiers: keep every statement of this transaction out of
        # the server log, failed ones included.
        await connection.execute(text("SET LOCAL log_statement = 'none'"))
        await connection.execute(text("SET LOCAL log_min_duration_statement = -1"))
        await connection.execute(text("SET LOCAL log_min_error_statement = 'panic'"))
        await connection.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        await connection.execute(
            text("SELECT pg_catalog.pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
        )


async def _check_existing_roles(connection: AsyncConnection, names: RoleNames) -> None:
    """Refuse to reuse a superuser, or a role with any membership but app -> runtime group.

    Read after the advisory lock: a concurrent bootstrap has committed by now.
    """
    superusers = (
        await connection.scalars(
            text(
                "SELECT rolname FROM pg_catalog.pg_roles "
                "WHERE rolname = ANY(:names) AND rolsuper ORDER BY rolname"
            ),
            {"names": list(names.all())},
        )
    ).all()
    if superusers:
        raise BootstrapError(f"refusing to reuse superuser role(s): {', '.join(superusers)}")

    memberships = (
        await connection.execute(
            text(
                "SELECT DISTINCT m.rolname AS member, g.rolname AS role "
                "FROM pg_catalog.pg_auth_members am "
                "JOIN pg_catalog.pg_roles g ON g.oid = am.roleid "
                "JOIN pg_catalog.pg_roles m ON m.oid = am.member "
                "WHERE g.rolname = ANY(:names) OR m.rolname = ANY(:names) "
                "ORDER BY member, role"
            ),
            {"names": list(names.all())},
        )
    ).all()
    unexpected = [
        f"{row.member} in {row.role}"
        for row in memberships
        if (row.member, row.role) != (names.app, names.runtime_group)
    ]
    if unexpected:
        raise BootstrapError(f"refusing unexpected role membership(s): {'; '.join(unexpected)}")


async def _check_role_ownerships(connection: AsyncConnection, names: RoleNames) -> None:
    """Refuse a target role that owns what it must not.

    The app and the runtime group own nothing at all: not this database, no schema, no object
    in it, no shared object (another database, a tablespace). An owner has every privilege on
    what it owns, which no REVOKE takes away. The owner role owns the objects of this database
    but no shared object: owning a database, this one included, would make it the owner of
    that database's public schema (pg_database_owner) and let it grant on it.

    pg_shdepend records every ownership except the bootstrap superuser's (OID 10), never a
    target role, so it covers every kind of object without listing catalogs.
    """
    owned = (
        await connection.execute(
            text(
                "SELECT r.rolname AS role, "
                "pg_catalog.pg_describe_object(s.classid, s.objid, s.objsubid) AS object "
                "FROM pg_catalog.pg_shdepend s "
                "JOIN pg_catalog.pg_roles r ON r.oid = s.refobjid "
                "WHERE s.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass "
                "AND s.deptype = 'o' "
                "AND ((r.rolname = ANY(:runtime) AND s.dbid IN (0, ("
                "SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()))) "
                "OR (r.rolname = :owner AND s.dbid = 0)) "
                "ORDER BY 1, 2"
            ),
            {"runtime": [names.app, names.runtime_group], "owner": names.owner},
        )
    ).all()
    if owned:
        listing = "; ".join(f"{row.role} owns {row.object}" for row in owned)
        raise BootstrapError(f"refusing role(s) that own what they must not: {listing}")


async def _check_default_privileges(connection: AsyncConnection, names: RoleNames) -> None:
    """Refuse default privileges (ALTER DEFAULT PRIVILEGES) in this database that would grant
    future objects to PUBLIC, the app or the runtime group, or that the app or the runtime
    group define. Such a default silently grants every table a later migration creates,
    bypassing migration 0013's explicit grants. (A default that only REVOKEs, e.g. PUBLIC's
    EXECUTE on new functions, grants nothing and is accepted.)"""
    runtime = [names.app, names.runtime_group]
    defaults = (
        await connection.execute(
            text(
                "SELECT pg_catalog.pg_get_userbyid(d.defaclrole) AS creator, "
                "CASE WHEN d.defaclnamespace = 0 THEN 'every schema' "
                "ELSE 'schema ' || d.defaclnamespace::pg_catalog.regnamespace::text END AS scope, "
                "CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
                "ELSE pg_catalog.pg_get_userbyid(a.grantee) END AS grantee "
                "FROM pg_catalog.pg_default_acl d "
                "LEFT JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) a ON true "
                "WHERE pg_catalog.pg_get_userbyid(d.defaclrole) = ANY(:runtime) "
                "OR a.grantee = 0 OR pg_catalog.pg_get_userbyid(a.grantee) = ANY(:runtime) "
                "ORDER BY 1, 2, 3"
            ),
            {"runtime": runtime},
        )
    ).all()
    if defaults:
        listing = "; ".join(
            f"objects of {row.creator} in {row.scope} granted to {row.grantee}" for row in defaults
        )
        raise BootstrapError(f"refusing default privileges: {listing}")


# --- 2. roles and membership (cluster-wide) ----------------------------------------------------


async def _reconcile_roles(
    connection: AsyncConnection, names: RoleNames, owner_verifier: str, app_verifier: str
) -> tuple[str, ...]:
    created: list[str] = []
    roles: Sequence[tuple[str, str, str | None]] = (
        # (name, login and inherit attributes, password verifier)
        (names.runtime_group, "NOLOGIN NOINHERIT", None),
        (names.owner, "LOGIN NOINHERIT", owner_verifier),
        (names.app, "LOGIN INHERIT", app_verifier),
    )
    for name, attributes, verifier in roles:
        async with _step(f"configuring role {name}"):
            if not await _role_exists(connection, name):
                await _execute_formatted(connection, "CREATE ROLE %I", name)
                created.append(name)
            await _execute_formatted(
                connection, f"ALTER ROLE %I WITH {attributes} {SAFE_ATTRIBUTES}", name
            )
        if verifier is None:
            async with _step(f"clearing the password of role {name}"):
                await _execute_formatted(connection, "ALTER ROLE %I WITH PASSWORD NULL", name)
        else:
            code = await _set_password(connection, name, verifier)
            if code is not None:
                # Raised here, outside any except block: the error has no __context__, so the
                # database error (whose parameters hold the verifier) is not kept anywhere.
                raise BootstrapError(
                    f"setting the password of role {name} failed (SQLSTATE {code})"
                )
    return tuple(created)


# Builds and runs ALTER ROLE ... PASSWORD on the server, from two transaction-local settings.
# The text is fixed: the verifier is never part of any statement the client sends, so neither
# SQL logging (echo) nor an error's [SQL: ...] can show it; it travels only as a bound value.
# A failure of the ALTER is replaced by a fixed message with the same SQLSTATE: re-raised from
# the handler, its context is this block, not the ALTER statement (which quotes the verifier).
SET_PASSWORD = text(
    "DO $$ BEGIN "
    "EXECUTE pg_catalog.format('ALTER ROLE %I WITH PASSWORD %L', "
    "pg_catalog.current_setting('ledger_bootstrap.role'), "
    "pg_catalog.current_setting('ledger_bootstrap.verifier')); "
    "EXCEPTION WHEN OTHERS THEN "
    "RAISE EXCEPTION 'setting a role password failed' USING ERRCODE = SQLSTATE; "
    "END $$"
)


async def _set_password(connection: AsyncConnection, role: str, verifier: str) -> str | None:
    """Set `role`'s password to `verifier`. Returns None, or the SQLSTATE of the failure: the
    database error itself is dropped here, never raised or chained."""
    try:
        # is_local: the settings vanish at the end of the transaction, whatever happens.
        await connection.execute(
            text(
                "SELECT pg_catalog.set_config('ledger_bootstrap.role', :role, true), "
                "pg_catalog.set_config('ledger_bootstrap.verifier', :verifier, true)"
            ),
            {"role": role, "verifier": verifier},
        )
        await connection.execute(SET_PASSWORD)
        await connection.execute(
            text("SELECT pg_catalog.set_config('ledger_bootstrap.verifier', '', true)")
        )
    except DBAPIError as error:
        return sqlstate(error) or "unknown"
    return None


async def _reconcile_membership(connection: AsyncConnection, names: RoleNames) -> None:
    """app inherits the runtime group's privileges, but may not SET ROLE to it or grant it.

    Re-granting an existing membership updates its options in place (PostgreSQL 16+).
    """
    async with _step("granting the runtime group"):
        await _execute_formatted(
            connection,
            "GRANT %I TO %I WITH INHERIT TRUE, SET FALSE, ADMIN FALSE",
            names.runtime_group,
            names.app,
        )
        # A membership granted by another grantor is a separate row with its own options.
        loose = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_catalog.pg_auth_members "
                "WHERE roleid = to_regrole(:role) AND member = to_regrole(:member) "
                "AND NOT (inherit_option AND NOT set_option AND NOT admin_option)"
            ),
            {"role": names.runtime_group, "member": names.app},
        )
    if loose:
        raise BootstrapError(
            f"role {names.app} holds {names.runtime_group} with other options from another "
            "grantor: revoke that grant first"
        )


# --- 3. this database and its public schema ----------------------------------------------------


async def _restrict_database(connection: AsyncConnection, names: RoleNames, database: str) -> None:
    """Only the two login roles may connect; nobody else gets CONNECT or TEMPORARY (temporary
    tables are how a session shadows unqualified names, ADR 0012)."""
    async with _step(f"restricting database {database}"):
        await _execute_formatted(connection, "REVOKE ALL ON DATABASE %I FROM PUBLIC", database)
        await _execute_formatted(
            connection, "REVOKE ALL ON DATABASE %I FROM %I, %I, %I", database, *names.all()
        )
        await _execute_formatted(
            connection, "GRANT CONNECT ON DATABASE %I TO %I, %I", database, names.owner, names.app
        )


async def _restrict_schema(connection: AsyncConnection, names: RoleNames) -> None:
    """The owner may create in public; the runtime group may only look names up in it. Table
    privileges are migration 0013's: the schema belongs to the database owner (the admin), so a
    migration, running as the owner role, could not grant on it."""
    async with _step("restricting schema public"):
        await connection.exec_driver_sql("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        await _execute_formatted(
            connection, "REVOKE ALL ON SCHEMA public FROM %I, %I, %I", *names.all()
        )
        await _execute_formatted(
            connection, "GRANT USAGE, CREATE ON SCHEMA public TO %I", names.owner
        )
        await _execute_formatted(
            connection, "GRANT USAGE ON SCHEMA public TO %I", names.runtime_group
        )


async def _check_access_control(connection: AsyncConnection, names: RoleNames) -> None:
    """The privileges PUBLIC and the three roles hold now, on this database and on public,
    whoever granted them, are exactly the intended ones.

    REVOKE run by a superuser acts as the object's owner, so it removes only grants that owner
    made: a grant made by another role holding the grant option (to PUBLIC, say) survives the
    REVOKEs above. Such a grant is refused, naming its grantor, rather than revoked here: who
    delegated it, and why, is for an operator to decide. Grants to any other role are left
    alone (they are not this bootstrap's).
    """
    intended = {
        ("database", names.owner, "CONNECT"),
        ("database", names.app, "CONNECT"),
        ("schema public", names.owner, "USAGE"),
        ("schema public", names.owner, "CREATE"),
        ("schema public", names.runtime_group, "USAGE"),
    }
    async with _step("checking database and schema privileges"):
        grants = (
            await connection.execute(
                text(
                    "SELECT g.object, "
                    "CASE WHEN g.grantee = 0 THEN 'PUBLIC' "
                    "ELSE pg_catalog.pg_get_userbyid(g.grantee) END AS grantee, "
                    "g.privilege_type, pg_catalog.pg_get_userbyid(g.grantor) AS grantor "
                    "FROM ("
                    "SELECT 'database' AS object, a.grantee, a.privilege_type, a.grantor "
                    "FROM pg_catalog.pg_database d, pg_catalog.aclexplode(d.datacl) a "
                    "WHERE d.datname = current_database() AND a.grantee <> d.datdba "
                    "UNION ALL "
                    "SELECT 'schema public', a.grantee, a.privilege_type, a.grantor "
                    "FROM pg_catalog.pg_namespace n, pg_catalog.aclexplode(n.nspacl) a "
                    "WHERE n.nspname = 'public' AND a.grantee <> n.nspowner"
                    ") g ORDER BY 1, 2, 3, 4"
                )
            )
        ).all()
    governed = {"PUBLIC", *names.all()}
    unintended = [
        f"{row.privilege_type} on {row.object} to {row.grantee} (granted by {row.grantor})"
        for row in grants
        if row.grantee in governed and (row.object, row.grantee, row.privilege_type) not in intended
    ]
    if unintended:
        raise BootstrapError(f"refusing unintended privileges: {'; '.join(unintended)}")


# --- 4. ownership of the objects in public -----------------------------------------------------

# Every object in public that has an owner of its own, with the format() template that transfers
# it (NULL: a kind this bootstrap does not transfer, so it is refused rather than missed). Not
# listed, because they follow their table: indexes, TOAST tables, identity and serial sequences
# (PostgreSQL refuses to move those separately), row types and array types. Members of an
# extension are skipped: the extension owns them.
_OWNED_OBJECTS = text(
    """
    WITH extension_members AS (
        SELECT classid, objid FROM pg_catalog.pg_depend WHERE deptype = 'e'
    ), objects AS (
        SELECT CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'table'
                              WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view'
                              WHEN 'f' THEN 'foreign table' WHEN 'S' THEN 'sequence'
                              ELSE 'type' END AS kind,
               c.relowner AS owner,
               CASE c.relkind
                   WHEN 'r' THEN 'ALTER TABLE public.%I OWNER TO %I'
                   WHEN 'p' THEN 'ALTER TABLE public.%I OWNER TO %I'
                   WHEN 'v' THEN 'ALTER VIEW public.%I OWNER TO %I'
                   WHEN 'm' THEN 'ALTER MATERIALIZED VIEW public.%I OWNER TO %I'
                   WHEN 'f' THEN 'ALTER FOREIGN TABLE public.%I OWNER TO %I'
                   WHEN 'S' THEN 'ALTER SEQUENCE public.%I OWNER TO %I'
                   WHEN 'c' THEN 'ALTER TYPE public.%I OWNER TO %I'
               END AS template,
               c.relname::text AS argument,
               'public.' || pg_catalog.quote_ident(c.relname) AS name
          FROM pg_catalog.pg_class c
         WHERE c.relnamespace = 'public'::regnamespace
           AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S', 'c')
           AND (c.tableoid, c.oid) NOT IN (SELECT classid, objid FROM extension_members)
           AND NOT (c.relkind = 'S' AND EXISTS (
                   SELECT 1 FROM pg_catalog.pg_depend d
                    WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
                      AND d.refclassid = 'pg_catalog.pg_class'::regclass
                      AND d.deptype IN ('a', 'i')))
        UNION ALL
        SELECT CASE p.prokind WHEN 'f' THEN 'function' WHEN 'p' THEN 'procedure'
                              ELSE 'routine' END,
               p.proowner,
               CASE WHEN p.prokind IN ('f', 'p') THEN 'ALTER ROUTINE %s OWNER TO %I' END,
               'public.' || pg_catalog.quote_ident(p.proname)
                   || '(' || pg_catalog.pg_get_function_identity_arguments(p.oid) || ')',
               'public.' || pg_catalog.quote_ident(p.proname)
                   || '(' || pg_catalog.pg_get_function_identity_arguments(p.oid) || ')'
          FROM pg_catalog.pg_proc p
         WHERE p.pronamespace = 'public'::regnamespace
           AND (p.tableoid, p.oid) NOT IN (SELECT classid, objid FROM extension_members)
        UNION ALL
        -- Standalone types. Row types (typtype c) follow their relation, array types their
        -- element type, multiranges their range type.
        SELECT CASE t.typtype WHEN 'd' THEN 'domain' ELSE 'type' END,
               t.typowner,
               CASE t.typtype WHEN 'd' THEN 'ALTER DOMAIN public.%I OWNER TO %I'
                              WHEN 'e' THEN 'ALTER TYPE public.%I OWNER TO %I'
                              WHEN 'r' THEN 'ALTER TYPE public.%I OWNER TO %I' END,
               t.typname::text,
               'public.' || pg_catalog.quote_ident(t.typname)
          FROM pg_catalog.pg_type t
         WHERE t.typnamespace = 'public'::regnamespace
           AND t.typtype IN ('b', 'd', 'e', 'r')
           AND t.typelem = 0
           AND (t.tableoid, t.oid) NOT IN (SELECT classid, objid FROM extension_members)
        UNION ALL
        -- Kinds never created by this project's migrations: refused if owned by the admin.
        SELECT 'other', o.owner, NULL, NULL, 'public.' || pg_catalog.quote_ident(o.name)
          FROM (
                    SELECT oprname::text AS name, oprowner AS owner, oprnamespace AS namespace,
                           tableoid, oid FROM pg_catalog.pg_operator
          UNION ALL SELECT collname, collowner, collnamespace, tableoid, oid
                      FROM pg_catalog.pg_collation
          UNION ALL SELECT conname, conowner, connamespace, tableoid, oid
                      FROM pg_catalog.pg_conversion
          UNION ALL SELECT cfgname, cfgowner, cfgnamespace, tableoid, oid
                      FROM pg_catalog.pg_ts_config
          UNION ALL SELECT dictname, dictowner, dictnamespace, tableoid, oid
                      FROM pg_catalog.pg_ts_dict
          UNION ALL SELECT stxname, stxowner, stxnamespace, tableoid, oid
                      FROM pg_catalog.pg_statistic_ext
          UNION ALL SELECT opcname, opcowner, opcnamespace, tableoid, oid
                      FROM pg_catalog.pg_opclass
          UNION ALL SELECT opfname, opfowner, opfnamespace, tableoid, oid
                      FROM pg_catalog.pg_opfamily
          ) o
         WHERE o.namespace = 'public'::regnamespace
           AND (o.tableoid, o.oid) NOT IN (SELECT classid, objid FROM extension_members)
    )
    SELECT kind, name, pg_catalog.pg_get_userbyid(owner) AS owner, template, argument
      FROM objects
     ORDER BY kind, name
    """
)


async def _transfer_ownership(connection: AsyncConnection, names: RoleNames) -> tuple[str, ...]:
    """Give every object in public owned by the admin to the owner role. Refuse objects owned
    by anyone else (an unexplained owner is not this bootstrap's to take over), and kinds it
    cannot transfer. Objects the owner already has are left alone."""
    async with _step("listing the objects in schema public"):
        admin = await connection.scalar(text("SELECT current_user"))
        objects = (await connection.execute(_OWNED_OBJECTS)).all()

    foreign = [
        f"{o.kind} {o.name} (owner {o.owner})"
        for o in objects
        if o.owner not in (admin, names.owner)
    ]
    if foreign:
        raise BootstrapError(
            f"refusing to take over objects in public owned by other roles: {'; '.join(foreign)}"
        )
    to_transfer = [o for o in objects if o.owner == admin]
    unsupported = [f"{o.kind} {o.name}" for o in to_transfer if o.template is None]
    if unsupported:
        raise BootstrapError(f"cannot transfer these objects in public: {'; '.join(unsupported)}")

    for o in to_transfer:
        async with _step(f"transferring {o.kind} {o.name} to {names.owner}"):
            await _execute_formatted(connection, o.template, o.argument, names.owner)
    return tuple(f"{o.kind} {o.name}" for o in to_transfer)


# --- 5. post-conditions, checked before COMMIT -------------------------------------------------


async def _check_ownership(connection: AsyncConnection, names: RoleNames) -> None:
    """Everything in public now belongs to the owner role: the listed objects and also what
    follows them (indexes, identity sequences, row types)."""
    async with _step("checking ownership in schema public"):
        listed = [
            f"{o.kind} {o.name} (owner {o.owner})"
            for o in (await connection.execute(_OWNED_OBJECTS)).all()
            if o.owner != names.owner
        ]
        relations = (
            await connection.scalars(
                text(
                    "SELECT 'public.' || pg_catalog.quote_ident(c.relname) "
                    "|| ' (owner ' || pg_catalog.pg_get_userbyid(c.relowner) || ')' "
                    "FROM pg_catalog.pg_class c "
                    "WHERE c.relnamespace = 'public'::regnamespace "
                    "AND c.relowner <> to_regrole(:owner) "
                    "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d WHERE d.deptype = 'e' "
                    "AND d.classid = c.tableoid AND d.objid = c.oid) ORDER BY 1"
                ),
                {"owner": names.owner},
            )
        ).all()
    misowned = [*listed, *relations]
    if misowned:
        raise BootstrapError(f"objects in public not owned by {names.owner}: {'; '.join(misowned)}")


async def _check_sequence_helper(connection: AsyncConnection) -> None:
    """Migration 0007's helper, if migrated: still SECURITY DEFINER, owned by the owner of
    ledger_entries, with 0007's pinned search_path, and no EXECUTE for PUBLIC (a NULL ACL would
    mean PostgreSQL's default, which includes PUBLIC)."""
    async with _step("checking the sequence helper"):
        row = (
            await connection.execute(
                text(
                    "SELECT p.proowner = c.relowner AS owned_by_table_owner, "
                    "p.prosecdef AS security_definer, p.proconfig AS config, "
                    "p.proacl IS NOT NULL AND NOT EXISTS ("
                    "SELECT 1 FROM pg_catalog.aclexplode(p.proacl) a WHERE a.grantee = 0"
                    ") AS public_revoked "
                    "FROM pg_catalog.pg_proc p, pg_catalog.pg_class c "
                    "WHERE p.oid = to_regprocedure(:helper) AND c.oid = to_regclass(:table)"
                ),
                {"helper": SEQUENCE_HELPER, "table": ENTRIES_TABLE},
            )
        ).one_or_none()
    if row is None:
        return  # not migrated yet (a fresh database): nothing to check
    problems = [
        problem
        for problem, ok in (
            ("not owned by the owner of public.ledger_entries", row.owned_by_table_owner),
            ("not SECURITY DEFINER", row.security_definer),
            ("search_path not pinned to pg_catalog, pg_temp", row.config == PINNED_SEARCH_PATH),
            ("EXECUTE not revoked from PUBLIC", row.public_revoked),
        )
        if not ok
    ]
    if problems:
        raise BootstrapError(f"{SEQUENCE_HELPER}: {'; '.join(problems)}")


# --- command -----------------------------------------------------------------------------------


async def _run(settings: BootstrapSettings) -> BootstrapReport:
    engine = create_admin_engine(settings.admin_database_url.get_secret_value())
    try:
        async with engine.connect() as connection:
            return await bootstrap(
                connection,
                DEFAULT_ROLE_NAMES,
                owner_password=settings.ledger_owner_password,
                app_password=settings.ledger_app_password,
            )
    finally:
        await engine.dispose()


def main() -> int:
    """Exit status:

    0  bootstrapped (committed);
    1  refused or failed: nothing was changed, except when COMMIT itself failed, in which case
       the outcome is unknown (the message says so); either way, running it again is safe;
    2  misconfigured (environment variables), before any connection.

    Output never contains the admin URL, a password or a verifier. An unexpected exception
    (a bug) propagates with its traceback: no message raised here quotes a secret."""
    try:
        settings = BootstrapSettings()
    except ValidationError as error:
        # hide_input_in_errors: the message names the fields, never their values.
        print(f"bootstrap: invalid configuration\n{error}", file=sys.stderr)
        return 2
    try:
        report = asyncio.run(_run(settings))
    except BootstrapError as error:
        print(f"bootstrap refused: {error}", file=sys.stderr)
        return 1
    except (OSError, DBAPIError) as error:
        # Connecting failed (unreachable, authentication, unknown database), or COMMIT did, in
        # which case the outcome is unknown. Never str(error): it can name the host or the user.
        code = sqlstate(error) if isinstance(error, DBAPIError) else None
        detail = type(error).__name__ + (f", SQLSTATE {code}" if code else "")
        print(
            f"bootstrap failed: database error ({detail}); if COMMIT itself failed, the outcome "
            "is unknown; run it again either way (it is idempotent)",
            file=sys.stderr,
        )
        return 1
    created = ", ".join(report.created_roles) or "none"
    print(
        f"bootstrap complete for database {report.database}: roles created: {created}; "
        f"objects transferred to {DEFAULT_ROLE_NAMES.owner}: {len(report.transferred_objects)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
