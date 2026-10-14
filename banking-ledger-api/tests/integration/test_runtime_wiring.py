"""The production sequence, end to end, with the real role names (Phase 10 E6, ADR 0015):

    bootstrap CLI (admin) -> alembic CLI (ledger_owner) -> API (ledger_app)

The real names (ledger_owner, ledger_app, ledger_runtime) are cluster-wide roles: they are used
only here, on the disposable test cluster (tests.database refuses any other), and stay there,
in its tmpfs data, until the container goes. Each database is a disposable one of its own.
"""

import asyncio
import secrets
from collections.abc import AsyncIterator

import pytest
from alembic import command
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.admin import bootstrap as bootstrap_module
from ledger_api.admin.bootstrap import DEFAULT_ROLE_NAMES
from ledger_api.main import create_app
from tests.auth_helpers import TEST_PASSWORD
from tests.conftest import serve, settings_for
from tests.database import assert_reconciled, configured_database_url, run_alembic
from tests.integration.test_migration_guard import _alembic_cli
from tests.integration.test_role_bootstrap import (
    Cluster,
    _engine,
    _owners_in_public,
    _query,
    _query_commit,
    _scalar,
)

OWNER_PASSWORD = "synthetic-real-owner-password"  # noqa: S105 (synthetic test value)
APP_PASSWORD = "synthetic-real-app-password-1"  # noqa: S105 (synthetic test value)


@pytest.fixture
async def cluster() -> AsyncIterator[Cluster]:
    cluster = Cluster(configured_database_url())
    try:
        yield cluster
    finally:
        await cluster.cleanup()  # its databases; the real roles stay on the disposable cluster


def _owner(url: URL) -> URL:
    return url.set(username=DEFAULT_ROLE_NAMES.owner, password=OWNER_PASSWORD)


def _app(url: URL) -> URL:
    return url.set(username=DEFAULT_ROLE_NAMES.app, password=APP_PASSWORD)


async def _run_bootstrap_cli(
    url: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    """python -m ledger_api.admin.bootstrap, configured as the bootstrap service is."""
    monkeypatch.setenv("ADMIN_DATABASE_URL", url.render_as_string(hide_password=False))
    monkeypatch.setenv("LEDGER_OWNER_PASSWORD", OWNER_PASSWORD)
    monkeypatch.setenv("LEDGER_APP_PASSWORD", APP_PASSWORD)
    # main() runs its own event loop: in a thread, away from the test's.
    assert await asyncio.to_thread(bootstrap_module.main) == 0
    out, err = capsys.readouterr()
    for secret in (OWNER_PASSWORD, APP_PASSWORD, url.password):
        assert secret is not None
        assert secret not in out + err
    return out


async def _migrate_as_owner(url: URL) -> None:
    result = await asyncio.to_thread(_alembic_cli, _owner(url), "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert OWNER_PASSWORD not in result.stdout + result.stderr


async def _assert_owned_by_ledger_owner(url: URL) -> None:
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == "0013"
    assert await _owners_in_public(url) == {DEFAULT_ROLE_NAMES.owner}
    roles = {
        row.rolname: row
        for row in await _query(
            url,
            "SELECT rolname, rolsuper, rolcanlogin FROM pg_roles WHERE rolname = ANY(:n)",
            n=list(DEFAULT_ROLE_NAMES.all()),
        )
    }
    assert {name: (r.rolsuper, r.rolcanlogin) for name, r in roles.items()} == {
        DEFAULT_ROLE_NAMES.owner: (False, True),
        DEFAULT_ROLE_NAMES.app: (False, True),
        DEFAULT_ROLE_NAMES.runtime_group: (False, False),
    }


async def _use_the_api_as_ledger_app(url: URL) -> str:
    """Register, log in, open an account, post a keyed deposit and read it back, through an app
    built as uvicorn builds it (privilege guard on), connected as ledger_app."""
    app = create_app(settings_for(_app(url)))
    token = secrets.token_hex(4)
    email = f"wiring-{token}@example.com"
    async with serve(app) as client:
        async with app.state.engine.connect() as connection:
            identity = tuple(
                (await connection.execute(text("SELECT current_user, session_user"))).one()
            )
        assert identity == (DEFAULT_ROLE_NAMES.app, DEFAULT_ROLE_NAMES.app)

        created = await client.post("/users", json={"email": email, "password": TEST_PASSWORD})
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]
        login = await client.post("/sessions", json={"email": email, "password": TEST_PASSWORD})
        assert login.status_code == 201, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        opened = await client.post(
            f"/users/{user_id}/accounts", json={"currency": "GBP"}, headers=headers
        )
        assert opened.status_code == 201, opened.text
        base = f"/users/{user_id}/accounts/{opened.json()['id']}"
        keyed = {**headers, "Idempotency-Key": f"wiring-{token}"}
        deposit = await client.post(f"{base}/deposits", json={"amount_minor": 2_500}, headers=keyed)
        assert deposit.status_code == 201, deposit.text
        replay = await client.post(f"{base}/deposits", json={"amount_minor": 2_500}, headers=keyed)
        assert replay.headers.get("Idempotent-Replayed") == "true"
        statement = await client.get(f"{base}/statement", headers=headers)
        assert statement.json()["balance_minor"] == 2_500
    return str(user_id)


async def _assert_consistent_and_audited(url: URL, user_id: str) -> None:
    engine = _engine(url)  # the admin, to check what the application may not read
    try:
        async with AsyncSession(engine) as session, session.begin():
            await assert_reconciled(session)
    finally:
        await engine.dispose()
    actions = await _query(
        url,
        "SELECT action FROM audit_events WHERE actor_user_id = CAST(:u AS uuid) ORDER BY id",
        u=user_id,
    )
    assert [row.action for row in actions] == [
        "user.register",
        "auth.login",
        "account.open",
        "posting.deposit",
    ]


async def test_a_fresh_database_bootstrapped_migrated_and_served(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = await cluster.database("wiring_fresh")

    await _run_bootstrap_cli(url, monkeypatch, capsys)
    await _migrate_as_owner(url)
    await _assert_owned_by_ledger_owner(url)
    user_id = await _use_the_api_as_ledger_app(url)
    await _assert_consistent_and_audited(url, user_id)

    # Running the sequence again changes nothing (both steps are idempotent).
    again = await _run_bootstrap_cli(url, monkeypatch, capsys)
    assert "roles created: none" in again
    assert "objects transferred to ledger_owner: 0" in again
    await _migrate_as_owner(url)
    await _assert_owned_by_ledger_owner(url)


async def test_an_existing_volume_upgraded_in_place(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Today's volumes: migrated to 0012 by the admin superuser, with data in them.
    url = await cluster.database("wiring_existing")
    engine = _engine(url)
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0012"))
    finally:
        await engine.dispose()
    await _query_commit(url, "INSERT INTO users (email) VALUES ('existing-user@example.com')")

    out = await _run_bootstrap_cli(url, monkeypatch, capsys)
    assert "objects transferred to ledger_owner: 0" not in out  # everything was the admin's
    await _migrate_as_owner(url)

    await _assert_owned_by_ledger_owner(url)
    assert (
        await _scalar(url, "SELECT count(*) FROM users WHERE email = 'existing-user@example.com'")
        == 1
    )
    user_id = await _use_the_api_as_ledger_app(url)
    await _assert_consistent_and_audited(url, user_id)
