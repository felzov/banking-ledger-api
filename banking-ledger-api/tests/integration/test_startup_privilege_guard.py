"""The API refuses to serve as a privileged database role (Phase 10 E6, ADR 0015).

Every app here is built with the guard ON, as uvicorn builds it. Each case logs in as a real
role and checks what the server says about it (session_user/current_user, attributes,
memberships, ownership), never the URL: the same database, reached as different roles.

Roles are cluster-wide: each case uses a unique throwaway login role, dropped afterwards.
"""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import URL, text

from ledger_api.data import engine as engine_module
from ledger_api.domain.errors import TemporarilyUnavailableError
from ledger_api.main import create_app
from tests.auth_helpers import TEST_PASSWORD
from tests.conftest import serve, settings_for
from tests.database import RUNTIME_GROUP, configured_database_url
from tests.integration.test_role_bootstrap import Cluster, _query_commit
from tests.integration.test_runtime_privileges import RuntimeDatabase, _deploy

ROLE_PASSWORD = "synthetic-guard-password-01"  # noqa: S105 (synthetic test value)
# The same body as any unreachable database: nothing about the role reaches the client.
GENERIC_503 = {
    "code": TemporarilyUnavailableError.code,
    "detail": TemporarilyUnavailableError.message,
}


@dataclass(frozen=True)
class Deployment:
    database: RuntimeDatabase
    cluster: Cluster


@pytest.fixture(scope="module")
async def deployment() -> AsyncIterator[Deployment]:
    cluster = Cluster(configured_database_url())
    try:
        yield Deployment(await _deploy(cluster, "startup_guard"), cluster)
    finally:
        await cluster.cleanup()


async def _login_role(deployment: Deployment, *privileges: str) -> URL:
    """A unique login role like ledger_app (a member of the runtime group, CONNECT), plus the
    given privileges: each statement is formatted with {role}."""
    role = deployment.cluster.role()
    database = deployment.database
    await _query_commit(
        database.url,
        f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{ROLE_PASSWORD}'",
        f'GRANT "{RUNTIME_GROUP}" TO "{role}" WITH INHERIT TRUE, SET FALSE',
        f'GRANT CONNECT ON DATABASE "{database.url.database}" TO "{role}"',
        *(privilege.format(role=role) for privilege in privileges),
    )
    return database.url.set(username=role, password=ROLE_PASSWORD)


@dataclass(frozen=True)
class Served:
    live: Response
    ready: Response
    register: Response


async def _serve_as(url: URL) -> Served:
    async with serve(create_app(settings_for(url))) as client:
        return Served(
            live=await client.get("/health/live"),
            ready=await client.get("/health/ready"),
            register=await client.post(
                "/users",
                json={
                    "email": f"guard-{secrets.token_hex(4)}@example.com",
                    "password": TEST_PASSWORD,
                },
            ),
        )


def _assert_refused(served: Served, caplog: pytest.LogCaptureFixture, reason: str) -> None:
    assert served.live.status_code == 200  # the process itself is fine
    assert (served.ready.status_code, served.ready.json()) == (503, {"status": "unavailable"})
    assert (served.register.status_code, served.register.json()) == (503, GENERIC_503)
    critical = [r.getMessage() for r in caplog.records if r.levelname == "CRITICAL"]
    assert critical
    assert all(message.startswith("refusing the database connection") for message in critical)
    assert any(reason in message for message in critical), critical


def _assert_no_secrets(caplog: pytest.LogCaptureFixture, url: URL) -> None:
    # The admin URL has no password in CI (trust auth keeps every credential out of git).
    if url.password is not None:
        assert url.password not in caplog.text
    assert url.render_as_string(hide_password=False) not in caplog.text


# --- refused ----------------------------------------------------------------------------------


async def test_the_admin_superuser_is_refused(
    deployment: Deployment, caplog: pytest.LogCaptureFixture
) -> None:
    url = deployment.database.url  # the bootstrap superuser
    served = await _serve_as(url)

    _assert_refused(served, caplog, f"{url.username} is a superuser")
    _assert_no_secrets(caplog, url)


async def test_the_owner_role_is_refused(
    deployment: Deployment, caplog: pytest.LogCaptureFixture
) -> None:
    url = deployment.database.owner_url  # ledger_owner's role: owns every object
    served = await _serve_as(url)

    _assert_refused(served, caplog, f"{url.username} owns objects in schema public")
    _assert_no_secrets(caplog, url)


@pytest.mark.parametrize(
    ("privileges", "reason"),
    [
        (('ALTER ROLE "{role}" CREATEDB',), "can create databases"),
        (('ALTER ROLE "{role}" CREATEROLE',), "can create roles"),
        (('ALTER ROLE "{role}" BYPASSRLS',), "bypasses row-level security"),
        (('ALTER ROLE "{role}" REPLICATION',), "can replicate"),
        # Through a membership: inherits the owner's ownership (and its power over triggers).
        (('GRANT "{owner}" TO "{role}"',), "owns objects in schema public"),
        # Membership in a predefined role that reads every table, audit_events included.
        (('GRANT pg_read_all_data TO "{role}"',), "is a member of pg_read_all_data"),
        # A SET-only membership (no inheritance) is enough: SET ROLE makes it a superuser.
        (
            ('GRANT "{admin}" TO "{role}" WITH INHERIT FALSE, SET TRUE',),
            "is a member of {admin}",
        ),
    ],
    ids=[
        "createdb",
        "createrole",
        "bypassrls",
        "replication",
        "owner-member",
        "read-all-data",
        "set-superuser",
    ],
)
async def test_privileged_roles_are_refused(
    deployment: Deployment,
    caplog: pytest.LogCaptureFixture,
    privileges: tuple[str, ...],
    reason: str,
) -> None:
    admin = deployment.database.url.username
    assert admin is not None
    names = {"owner": deployment.database.names.owner, "admin": admin}
    # {owner} and {admin} now; {role} by _login_role, once the role exists.
    url = await _login_role(
        deployment,
        *(p.replace("{owner}", names["owner"]).replace("{admin}", admin) for p in privileges),
    )
    served = await _serve_as(url)

    _assert_refused(served, caplog, reason.format(**names))
    _assert_no_secrets(caplog, url)


async def test_a_check_that_cannot_complete_refuses(
    deployment: Deployment, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail closed: an unprivileged role, but the check itself errors.
    monkeypatch.setattr(engine_module, "ROLE_PRIVILEGES", "SELECT * FROM no_such_catalog")
    served = await _serve_as(deployment.database.app_url)

    _assert_refused(served, caplog, "could not be verified")


# --- allowed ----------------------------------------------------------------------------------


async def test_the_app_role_is_served(
    deployment: Deployment, caplog: pytest.LogCaptureFixture
) -> None:
    served = await _serve_as(deployment.database.app_url)

    assert served.ready.status_code == 200
    assert served.register.status_code == 201, served.register.text
    assert not [r for r in caplog.records if r.levelname == "CRITICAL"]


async def test_the_check_runs_once_per_new_connection(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pooled connections are reused unchecked: the check was made when they were opened.
    calls = 0
    real = engine_module._role_privileges

    async def counted(connection: Any) -> list[Any]:
        nonlocal calls
        calls += 1
        return await real(connection)

    monkeypatch.setattr(engine_module, "_role_privileges", counted)
    app = create_app(settings_for(deployment.database.app_url))
    async with serve(app) as client:
        for _ in range(5):
            assert (await client.get("/health/ready")).status_code == 200
        # The server's view of the identity the API's pool connects as.
        async with app.state.engine.connect() as connection:
            identity = tuple(
                (await connection.execute(text("SELECT current_user, session_user"))).one()
            )

    assert calls == 1
    assert identity == (deployment.database.names.app, deployment.database.names.app)
