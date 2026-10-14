"""migrations/env.py refuses to migrate as a superuser (Phase 10 E6, ADR 0015).

The command line is exercised for real: `python -m alembic`, in a subprocess, configured only
by DATABASE_URL, exactly as the migrate service runs it. The only exemption is Python-only
(config.attributes, set by tests.database.run_alembic for the suite's own databases): neither an
environment variable nor an -x argument reaches it.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import URL, Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.database import ALEMBIC_INI, configured_database_url
from tests.integration.test_role_bootstrap import (
    OWNER_PASSWORD,
    Cluster,
    _as,
    _bootstrap,
    _owners_in_public,
    _scalar,
)

REFUSAL = "refusing to run migrations as a PostgreSQL superuser"
HEAD = "0013"


@pytest.fixture
async def cluster() -> AsyncIterator[Cluster]:
    cluster = Cluster(configured_database_url())
    try:
        yield cluster
    finally:
        await cluster.cleanup()


def _alembic_cli(
    url: URL, *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """`python -m alembic -c alembic.ini <arguments>` with this DATABASE_URL, as the migrate
    service runs it."""
    env = {
        **os.environ,
        "DATABASE_URL": url.render_as_string(hide_password=False),
        **(environment or {}),
    }
    return subprocess.run(  # noqa: S603 (fixed arguments, this interpreter)
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *arguments],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


async def _migrated(url: URL) -> bool:
    return bool(await _scalar(url, "SELECT to_regclass('public.alembic_version') IS NOT NULL"))


@pytest.mark.parametrize(
    ("arguments", "environment"),
    [
        (("upgrade", "head"), {}),
        # No environment variable bypasses the guard...
        (("upgrade", "head"), {"ALLOW_SUPERUSER_MIGRATIONS": "true"}),
        # ...and neither does an -x argument of the same name.
        (("-x", "allow_superuser_migrations=true", "upgrade", "head"), {}),
    ],
    ids=["plain", "environment-variable", "x-argument"],
)
async def test_the_command_line_refuses_a_superuser(
    cluster: Cluster, arguments: tuple[str, ...], environment: dict[str, str]
) -> None:
    url = await cluster.database("migrate_as_superuser")

    result = await asyncio.to_thread(_alembic_cli, url, *arguments, environment=environment)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert REFUSAL in output
    assert url.password is not None
    assert url.password not in output
    assert not await _migrated(url)  # refused before the first migration


async def test_the_command_line_migrates_as_the_owner(cluster: Cluster) -> None:
    url = await cluster.database("migrate_as_owner")
    names = cluster.role_names()
    await _bootstrap(url, names)

    result = await asyncio.to_thread(
        _alembic_cli, _as(url, names.owner, OWNER_PASSWORD), "upgrade", "head"
    )

    assert result.returncode == 0, result.stderr
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD
    # Created by the migrating role: it ran as the owner, which owns everything.
    assert await _owners_in_public(url) == {names.owner}


async def test_only_python_can_exempt_a_superuser(cluster: Cluster) -> None:
    url = await cluster.database("migrate_python_exemption")
    engine = create_async_engine(url, poolclass=NullPool)

    def upgrade(*, exempt: bool) -> None:
        async def run() -> None:
            async with engine.begin() as connection:

                def migrate(sync_connection: Connection) -> None:
                    config = Config(str(ALEMBIC_INI))
                    config.attributes["connection"] = sync_connection
                    if exempt:
                        config.attributes["allow_superuser_migrations"] = True
                    command.upgrade(config, "head")

                await connection.run_sync(migrate)

        asyncio.run(run())

    try:
        with pytest.raises(CommandError, match=REFUSAL):
            await asyncio.to_thread(upgrade, exempt=False)
        assert not await _migrated(url)
        await asyncio.to_thread(upgrade, exempt=True)
        assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD
    finally:
        await engine.dispose()
