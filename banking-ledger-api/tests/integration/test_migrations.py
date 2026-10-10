import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_api.data.base import Base
from ledger_api.data.errors import sqlstate, violated_constraint
from ledger_api.data.models import Account, Ledger
from ledger_api.domain.account import AccountKind
from ledger_api.domain.currency import Currency
from tests.database import (
    ALEMBIC_INI,
    configured_database_url,
    disposable_database_url,
    posting_role_grants,
    recreate_database,
    run_alembic,
)

LEDGER_TABLES = {"users", "ledgers", "accounts", "transactions", "ledger_entries", "audit_events"}


async def _public_tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return set(rows.scalars())


async def _public_functions(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT p.proname FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'"
            )
        )
        return set(rows.scalars())


SETTLEMENT_ACCOUNTS = text(
    "SELECT l.currency, a.user_id, a.balance_minor FROM accounts a "
    "JOIN ledgers l ON l.id = a.ledger_id WHERE a.kind = 'system'"
)


async def test_migrations_upgrade_downgrade_and_upgrade_again() -> None:
    # A database of its own: downgrading the shared test database would break other tests.
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_migrations")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "head"))
        assert await _public_tables(engine) == LEDGER_TABLES | {"alembic_version"}
        # Seeded, untouched: one settlement account per ledger, without an owner, at zero.
        # Checked here, on a fresh database: the shared test database holds committed postings.
        async with engine.connect() as connection:
            settlement = (await connection.execute(SETTLEMENT_ACCOUNTS)).all()
        assert sorted(settlement) == sorted((currency.value, None, 0) for currency in Currency)

        await run_alembic(engine, lambda config: command.downgrade(config, "base"))
        assert await _public_tables(engine) == {"alembic_version"}
        assert await _public_functions(engine) == set()

        await run_alembic(engine, lambda config: command.upgrade(config, "head"))
        assert await _public_tables(engine) == LEDGER_TABLES | {"alembic_version"}
    finally:
        await engine.dispose()


async def test_downgrade_refuses_to_drop_audit_evidence() -> None:
    # ADR 0011: a non-empty audit_events table is never dropped by a downgrade. The refusal
    # aborts the whole downgrade (one transaction), so the schema stays at head, intact.
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_audit_guard")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "head"))
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO audit_events (action, outcome, reason) "
                    "VALUES ('user.register', 'rejected', 'email_already_registered')"
                )
            )

        with pytest.raises(DBAPIError) as excinfo:
            await run_alembic(engine, lambda config: command.downgrade(config, "base"))

        assert sqlstate(excinfo.value) == "2BP01"  # dependent_objects_still_exist
        assert await _public_tables(engine) == LEDGER_TABLES | {"alembic_version"}
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM audit_events")) == 1
            version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
        assert version == head
    finally:
        await engine.dispose()


async def test_sequence_backfill_follows_id_order_and_new_entries_continue() -> None:
    # Migration 0005 numbers pre-existing entries in UUIDv7 id order (best-effort history:
    # no commit order was recorded before it), then hands out higher numbers. The rows are
    # stored in an order that differs from id order, so a backfill that followed the physical
    # order (or insertion order) instead would fail.
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_sequence_backfill")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    ids = [uuid.uuid7() for _ in range(4)]  # ascending: UUIDv7 is monotonic within a process
    assert ids == sorted(ids)
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0004"))
        async with engine.begin() as connection:
            # The later ids are written first.
            await _post_raw_deposit(connection, amount=100, entry_ids=(ids[3], ids[2]))
            await _post_raw_deposit(connection, amount=200, entry_ids=(ids[1], ids[0]))
        async with engine.connect() as connection:
            physical = (
                (await connection.execute(text("SELECT id FROM ledger_entries ORDER BY ctid")))
                .scalars()
                .all()
            )
        assert physical == [ids[3], ids[2], ids[1], ids[0]]  # the precondition really holds

        await run_alembic(engine, lambda config: command.upgrade(config, "head"))

        async with engine.begin() as connection:
            rows = await connection.execute(text("SELECT id, sequence_number FROM ledger_entries"))
            numbered: dict[uuid.UUID, int] = {row.id: row.sequence_number for row in rows}
            assert numbered == {ids[0]: 1, ids[1]: 2, ids[2]: 3, ids[3]: 4}
            await _post_raw_deposit(connection, amount=300)
            newest = (
                (
                    await connection.execute(
                        text(
                            "SELECT sequence_number FROM ledger_entries "
                            "ORDER BY sequence_number DESC LIMIT 2"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert sorted(newest) == [5, 6]
        async with engine.connect() as connection:
            # The backfill re-enabled the immutability trigger.
            with pytest.raises(DBAPIError) as excinfo:
                await connection.execute(text("UPDATE ledger_entries SET amount_minor = 1"))
            assert sqlstate(excinfo.value) == "23001"  # restrict_violation
    finally:
        await engine.dispose()


async def _post_raw_deposit(
    connection: AsyncConnection,
    *,
    amount: int,
    entry_ids: tuple[uuid.UUID, uuid.UUID] | None = None,
) -> None:
    """A balanced deposit written with plain SQL, as the schema of any revision accepts it.

    entry_ids: (settlement entry, customer entry), inserted in that order; generated if None.
    """
    ledger_id, settlement_id = (
        await connection.execute(
            text(
                "SELECT l.id, a.id FROM ledgers l JOIN accounts a "
                "ON a.ledger_id = l.id AND a.kind = 'system' WHERE l.currency = 'GBP'"
            )
        )
    ).one()
    user_id = await connection.scalar(
        text("INSERT INTO users (email) VALUES (:email) RETURNING id"),
        {"email": f"raw-{amount}@example.com"},
    )
    account_id = await connection.scalar(
        text(
            "INSERT INTO accounts (ledger_id, user_id, kind, balance_minor) "
            "VALUES (:ledger, :user, 'customer', :amount) RETURNING id"
        ),
        {"ledger": ledger_id, "user": user_id, "amount": amount},
    )
    await connection.execute(
        text("UPDATE accounts SET balance_minor = balance_minor - :amount WHERE id = :id"),
        {"amount": amount, "id": settlement_id},
    )
    transaction_id = await connection.scalar(
        text(
            "INSERT INTO transactions (ledger_id, kind, entry_count) "
            "VALUES (:ledger, 'deposit', 2) RETURNING id"
        ),
        {"ledger": ledger_id},
    )
    settlement_entry_id, customer_entry_id = entry_ids or (uuid.uuid7(), uuid.uuid7())
    await connection.execute(
        text(
            "INSERT INTO ledger_entries "
            "(id, transaction_id, account_id, ledger_id, amount_minor) "
            "VALUES (:se, :t, :s, :l, :debit), (:ce, :t, :a, :l, :credit)"
        ),
        {
            "se": settlement_entry_id,
            "ce": customer_entry_id,
            "t": transaction_id,
            "s": settlement_id,
            "a": account_id,
            "l": ledger_id,
            "debit": -amount,
            "credit": amount,
        },
    )


TRIGGER_FUNCTION_DEFINITION = text(
    "SELECT pg_get_functiondef('assert_entry_sequence_order'::regproc)"
)
HELPER_EXISTS = text(
    "SELECT to_regprocedure('public.ledger_entries_last_issued_sequence_number()') IS NOT NULL"
)


async def _insert_entries_as_new_role(connection: AsyncConnection, *, helper: bool) -> None:
    """In the caller's (rolled-back) transaction: a role with the posting privileges but none
    on the sequence inserts a normally numbered pair of entries."""
    ledger_id, settlement_id = (
        await connection.execute(
            text(
                "SELECT l.id, a.id FROM ledgers l JOIN accounts a "
                "ON a.ledger_id = l.id AND a.kind = 'system' WHERE l.currency = 'GBP'"
            )
        )
    ).one()
    user_id = await connection.scalar(
        text("INSERT INTO users (email) VALUES (:email) RETURNING id"),
        {"email": f"role-{uuid.uuid7().hex}@example.com"},
    )
    account_id = await connection.scalar(
        text(
            "INSERT INTO accounts (ledger_id, user_id, kind) "
            "VALUES (:ledger, :user, 'customer') RETURNING id"
        ),
        {"ledger": ledger_id, "user": user_id},
    )
    role = f"ledger_posting_{uuid.uuid7().hex}"
    await connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
    for grant in posting_role_grants(role, helper=helper):
        await connection.execute(text(grant))
    await connection.execute(text(f'SET LOCAL ROLE "{role}"'))
    transaction_id = await connection.scalar(
        text(
            "INSERT INTO transactions (ledger_id, kind, entry_count) "
            "VALUES (:ledger, 'deposit', 2) RETURNING id"
        ),
        {"ledger": ledger_id},
    )
    await connection.execute(
        text(
            "INSERT INTO ledger_entries (transaction_id, account_id, ledger_id, amount_minor) "
            "VALUES (:t, :s, :l, -1), (:t, :a, :l, 1)"
        ),
        {"t": transaction_id, "s": settlement_id, "a": account_id, "l": ledger_id},
    )


async def test_0007_lets_a_least_privilege_role_post_and_downgrades_exactly() -> None:
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_sequence_privileges")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0006"))
        async with engine.connect() as connection:
            definition_0006 = await connection.scalar(TRIGGER_FUNCTION_DEFINITION)

        # The bug, reproduced at 0006: the role cannot read the sequence, the trigger reads
        # NULL and rejects a perfectly normal entry as "never issued".
        async with engine.connect() as connection:
            with pytest.raises(IntegrityError) as excinfo:
                await _insert_entries_as_new_role(connection, helper=False)
            await connection.rollback()
        assert violated_constraint(excinfo.value) == "ck_ledger_entries_sequence_issued"

        await run_alembic(engine, lambda config: command.upgrade(config, "head"))
        async with engine.connect() as connection:
            await _insert_entries_as_new_role(connection, helper=True)  # accepted now
            await connection.rollback()
            assert await connection.scalar(HELPER_EXISTS) is True
            assert await connection.scalar(TRIGGER_FUNCTION_DEFINITION) != definition_0006

        await run_alembic(engine, lambda config: command.downgrade(config, "0006"))
        async with engine.connect() as connection:
            # 0006's function, restored byte for byte; nothing of 0007 left behind.
            assert await connection.scalar(TRIGGER_FUNCTION_DEFINITION) == definition_0006
            assert await connection.scalar(HELPER_EXISTS) is False

        await run_alembic(engine, lambda config: command.upgrade(config, "head"))
        async with engine.connect() as connection:
            assert await connection.scalar(HELPER_EXISTS) is True
    finally:
        await engine.dispose()


TRIGGER_FUNCTION_CONFIG = text(
    "SELECT proconfig FROM pg_proc WHERE oid = 'assert_entry_sequence_order'::regproc"
)


async def test_0008_pins_the_trigger_search_path_and_downgrades_exactly() -> None:
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_trigger_search_path")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0007"))
        async with engine.connect() as connection:
            definition_0007 = await connection.scalar(TRIGGER_FUNCTION_DEFINITION)
            assert await connection.scalar(TRIGGER_FUNCTION_CONFIG) is None

        await run_alembic(engine, lambda config: command.upgrade(config, "head"))
        async with engine.connect() as connection:
            assert await connection.scalar(TRIGGER_FUNCTION_CONFIG) == [
                "search_path=pg_catalog, pg_temp"
            ]
            definition_0008 = await connection.scalar(TRIGGER_FUNCTION_DEFINITION)
            assert "public.accounts" in definition_0008
            assert "public.ledger_entries" in definition_0008
            await _insert_entries_as_new_role(connection, helper=True)  # still posts normally
            await connection.rollback()

        await run_alembic(engine, lambda config: command.downgrade(config, "0007"))
        async with engine.connect() as connection:
            # 0007's function, restored byte for byte, without the pinned search_path.
            assert await connection.scalar(TRIGGER_FUNCTION_DEFINITION) == definition_0007
            assert await connection.scalar(TRIGGER_FUNCTION_CONFIG) is None

        await run_alembic(engine, lambda config: command.upgrade(config, "head"))
        async with engine.connect() as connection:
            assert await connection.scalar(TRIGGER_FUNCTION_DEFINITION) == definition_0008
    finally:
        await engine.dispose()


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    # Raises AutogenerateDiffsDetected if the models and the migrated schema have drifted
    # (tables, columns, types, nullability, foreign keys, unique constraints, indexes).
    await run_alembic(engine, command.check)


async def test_constraint_and_index_names_match_models(engine: AsyncEngine) -> None:
    # alembic check ignores CHECK constraints and does not compare names. Names matter: tests
    # and error handling identify violated rules by them.
    expected = {
        str(name)
        for table in Base.metadata.sorted_tables
        for name in [c.name for c in table.constraints] + [i.name for i in table.indexes]
    }
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT c.conname FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace "
                # Skip constraint triggers ('t') and, since PostgreSQL 18, NOT NULL ('n'):
                # nullability is already compared by alembic check.
                "WHERE n.nspname = 'public' AND c.contype NOT IN ('t', 'n') "
                "UNION "
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        )
        actual = set(rows.scalars()) - {"alembic_version_pkc"}

    assert actual == expected


async def test_seeded_ledgers_are_exactly_the_supported_currencies(
    db_session: AsyncSession,
) -> None:
    currencies = (await db_session.scalars(text("SELECT currency FROM ledgers"))).all()

    assert sorted(currencies) == sorted(Currency)


async def test_every_ledger_has_one_settlement_account_without_an_owner(
    db_session: AsyncSession,
) -> None:
    # Balances are not asserted here: committed tests that ran earlier have posted against
    # the settlement accounts. Their seeded zero balance is checked on a fresh database above.
    ledgers = (await db_session.scalars(select(Ledger))).all()
    system_accounts = (
        await db_session.scalars(select(Account).where(Account.kind == AccountKind.SYSTEM))
    ).all()

    assert len(system_accounts) == len(ledgers) == len(Currency)
    rows = (await db_session.execute(SETTLEMENT_ACCOUNTS)).all()
    assert sorted((currency, owner) for currency, owner, _ in rows) == sorted(
        (currency.value, None) for currency in Currency
    )
