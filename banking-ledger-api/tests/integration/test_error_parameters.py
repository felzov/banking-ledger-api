"""Bound SQL parameters stay out of database error messages (Phase 10 E1).

SQLAlchemy renders a failed statement's parameters into str(error) unless the engine sets
hide_parameters. That string reaches the logs: an unhandled error's traceback, the audit
fallback line (exc_info) and the readiness probe's warning. On the authentication paths the
parameters include Argon2id hashes, token digests and email addresses.

The errors come from the application's own engine factory (data.engine.create_engine). The
control test shows the same failure without hide_parameters does leak, so a passing test here
cannot be vacuous.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ledger_api.data.engine import create_engine
from ledger_api.data.errors import server_severity, sqlstate, violated_constraint
from tests.conftest import settings_for

# Shaped like what the authentication paths bind; synthetic.
SENTINEL_HASH = "$argon2id$v=19$m=8,t=1,p=1$c3ludGhldGlj$c3ludGhldGljLWhhc2gtdmFsdWU"
# Upper case violates ck_users_email_lowercase.
SENTINEL_EMAIL = "Synthetic.Secret@Example.COM"

# Fails on the server (22012) with a message that never quotes the bound value itself.
FAILING_STATEMENT = text("SELECT length(CAST(:secret AS text)) / 0")
HIDDEN_MARKER = "[SQL parameters hidden due to hide_parameters=True]"


@pytest.fixture
async def app_engine(engine: AsyncEngine, test_database_url: URL) -> AsyncIterator[AsyncEngine]:
    # Depends on `engine` so the test database exists and is migrated.
    app_engine = create_engine(settings_for(test_database_url))
    try:
        yield app_engine
    finally:
        await app_engine.dispose()


async def test_a_failed_statement_shows_its_sql_but_not_its_parameters(
    app_engine: AsyncEngine,
) -> None:
    async with app_engine.connect() as connection:
        with pytest.raises(DBAPIError) as excinfo:
            await connection.execute(FAILING_STATEMENT, {"secret": SENTINEL_HASH})

    message = str(excinfo.value)
    assert SENTINEL_HASH not in message
    assert HIDDEN_MARKER in message
    # Still diagnosable: the statement and PostgreSQL's own message.
    assert "[SQL: SELECT length(CAST($1 AS text)) / 0]" in message
    assert "division by zero" in message
    # Classification reads the driver's exception, not the rendered message: unchanged.
    assert sqlstate(excinfo.value) == "22012"
    assert server_severity(excinfo.value) == "ERROR"


async def test_a_constraint_violation_names_its_constraint_but_not_the_row(
    app_engine: AsyncEngine,
) -> None:
    async with app_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(IntegrityError) as excinfo:
                await connection.execute(
                    text("INSERT INTO users (email) VALUES (:email)"), {"email": SENTINEL_EMAIL}
                )
        finally:
            await transaction.rollback()

    message = str(excinfo.value)
    assert SENTINEL_EMAIL not in message
    assert HIDDEN_MARKER in message
    assert "ck_users_email_lowercase" in message
    # Services translate violations by exact constraint name: unchanged.
    assert violated_constraint(excinfo.value) == "ck_users_email_lowercase"
    assert sqlstate(excinfo.value) == "23514"


async def test_control_an_engine_without_hide_parameters_leaks_them(
    engine: AsyncEngine, test_database_url: URL
) -> None:
    # Depends on `engine` so the test database exists. Not the application's configuration:
    # shows what the two tests above would see if hide_parameters were removed.
    plain_engine = create_async_engine(test_database_url)
    try:
        async with plain_engine.connect() as connection:
            with pytest.raises(DBAPIError) as excinfo:
                await connection.execute(FAILING_STATEMENT, {"secret": SENTINEL_HASH})
    finally:
        await plain_engine.dispose()

    assert SENTINEL_HASH in str(excinfo.value)
