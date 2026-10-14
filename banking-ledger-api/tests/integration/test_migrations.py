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
from ledger_api.data.engine import create_engine, create_sessionmaker
from ledger_api.data.errors import sqlstate, violated_constraint
from ledger_api.data.models import Account, IdempotencyKey, Ledger
from ledger_api.domain.account import AccountKind
from ledger_api.domain.currency import Currency
from ledger_api.services.posting import withdraw
from tests.conftest import settings_for
from tests.database import (
    ALEMBIC_INI,
    assert_reconciled,
    configured_database_url,
    disposable_database_url,
    posting_role_grants,
    recreate_database,
    run_alembic,
)

LEDGER_TABLES = {
    "users",
    "ledgers",
    "accounts",
    "transactions",
    "ledger_entries",
    "audit_events",
    "idempotency_keys",
    "user_credentials",
    "auth_sessions",
}


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


# A committed deposit that consumed an idempotency key, in raw SQL: a frozen snapshot, like the
# migrations themselves. Every deferred check passes at COMMIT.
COMMITTED_KEYED_DEPOSIT = [
    "INSERT INTO users (id, email) VALUES ('{user}', 'keyed-{user}@example.test')",
    "INSERT INTO accounts (id, ledger_id, user_id, kind) "
    "SELECT '{account}', id, '{user}', 'customer' FROM ledgers WHERE currency = 'GBP'",
    "INSERT INTO transactions (id, ledger_id, kind, entry_count) "
    "SELECT '{transaction}', id, 'deposit', 2 FROM ledgers WHERE currency = 'GBP'",
    "INSERT INTO ledger_entries (transaction_id, account_id, ledger_id, amount_minor) "
    "SELECT '{transaction}', a.id, a.ledger_id, CASE a.kind WHEN 'system' THEN -100 ELSE 100 END "
    "FROM accounts a JOIN ledgers l ON l.id = a.ledger_id "
    "WHERE l.currency = 'GBP' AND (a.kind = 'system' OR a.id = '{account}')",
    "UPDATE accounts SET balance_minor = balance_minor + CASE kind WHEN 'system' THEN -100 "
    "ELSE 100 END WHERE id = '{account}' OR (kind = 'system' AND ledger_id = "
    "(SELECT id FROM ledgers WHERE currency = 'GBP'))",
    "INSERT INTO idempotency_keys (user_id, key, request_fingerprint, transaction_id) "
    "VALUES ('{user}', 'key-1', decode(repeat('ab', 32), 'hex'), '{transaction}')",
]


async def test_downgrade_refuses_to_drop_consumed_idempotency_keys() -> None:
    # Phase 6: dropping idempotency_keys would let retried requests run a second time. The
    # refusal aborts the downgrade (one transaction): the schema stays at 0010, keys intact.
    # At 0010 itself: from head, 0011's own guard would refuse first (its links exist).
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_idempotency_guard")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    ids = {"user": uuid.uuid7(), "account": uuid.uuid7(), "transaction": uuid.uuid7()}
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0010"))
        async with engine.begin() as connection:
            for statement in COMMITTED_KEYED_DEPOSIT:
                await connection.execute(text(statement.format(**ids)))

        with pytest.raises(DBAPIError) as excinfo:
            await run_alembic(engine, lambda config: command.downgrade(config, "0009"))

        assert sqlstate(excinfo.value) == "2BP01"  # dependent_objects_still_exist
        assert "idempotency_keys" in await _public_tables(engine)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM idempotency_keys")) == 1
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0010"
            )
    finally:
        await engine.dispose()


async def test_0010_downgrades_exactly_on_an_empty_table() -> None:
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_idempotency_round_trip")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0009"))
        tables_0009, functions_0009 = await _public_tables(engine), await _public_functions(engine)

        await run_alembic(engine, lambda config: command.upgrade(config, "0010"))
        assert await _public_tables(engine) == tables_0009 | {"idempotency_keys"}
        assert await _public_functions(engine) == functions_0009 | {
            "forbid_idempotency_key_mutation"
        }

        await run_alembic(engine, lambda config: command.downgrade(config, "0009"))
        assert await _public_tables(engine) == tables_0009
        assert await _public_functions(engine) == functions_0009
    finally:
        await engine.dispose()


# The same committed keyed deposit at 0011: the header names its key (both directions).
COMMITTED_LINKED_KEYED_DEPOSIT = [
    statement.replace(
        "INSERT INTO transactions (id, ledger_id, kind, entry_count) "
        "SELECT '{transaction}', id, 'deposit', 2 FROM ledgers WHERE currency = 'GBP'",
        "INSERT INTO transactions "
        "(id, ledger_id, kind, entry_count, idempotency_user_id, idempotency_key) "
        "SELECT '{transaction}', id, 'deposit', 2, '{user}', 'key-1' "
        "FROM ledgers WHERE currency = 'GBP'",
    )
    for statement in COMMITTED_KEYED_DEPOSIT
]
assert COMMITTED_LINKED_KEYED_DEPOSIT != COMMITTED_KEYED_DEPOSIT

TRANSACTION_COLUMNS = text(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'transactions' ORDER BY column_name"
)
TRANSACTION_CONSTRAINTS = text(
    "SELECT conname FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass "
    "AND contype <> 'n' ORDER BY conname"
)


async def test_0011_round_trips_exactly_on_an_empty_database() -> None:
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_link_round_trip")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0010"))
        functions_0010 = await _public_functions(engine)
        async with engine.connect() as connection:
            columns_0010 = list((await connection.execute(TRANSACTION_COLUMNS)).scalars())
            constraints_0010 = list((await connection.execute(TRANSACTION_CONSTRAINTS)).scalars())

        await run_alembic(engine, lambda config: command.upgrade(config, "0011"))
        assert await _public_functions(engine) == functions_0010 | {"assert_idempotency_link"}
        async with engine.connect() as connection:
            columns = set((await connection.execute(TRANSACTION_COLUMNS)).scalars())
            constraints = set((await connection.execute(TRANSACTION_CONSTRAINTS)).scalars())
        assert columns == set(columns_0010) | {"idempotency_user_id", "idempotency_key"}
        assert constraints == set(constraints_0010) | {
            "ck_transactions_idempotency_key_pair",
            "uq_transactions_idempotency_key",
            "fk_transactions_idempotency_keys",
            "transactions_idempotency_linked",  # a constraint trigger is listed here too
        }

        await run_alembic(engine, lambda config: command.downgrade(config, "0010"))
        assert await _public_functions(engine) == functions_0010
        async with engine.connect() as connection:
            assert list((await connection.execute(TRANSACTION_COLUMNS)).scalars()) == columns_0010
            assert (
                list((await connection.execute(TRANSACTION_CONSTRAINTS)).scalars())
                == constraints_0010
            )
    finally:
        await engine.dispose()


async def test_0011_downgrade_refuses_to_drop_idempotency_links() -> None:
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_link_guard")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    ids = {"user": uuid.uuid7(), "account": uuid.uuid7(), "transaction": uuid.uuid7()}
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0011"))
        async with engine.begin() as connection:
            for statement in COMMITTED_LINKED_KEYED_DEPOSIT:
                await connection.execute(text(statement.format(**ids)))

        with pytest.raises(DBAPIError) as excinfo:
            await run_alembic(engine, lambda config: command.downgrade(config, "0010"))

        assert sqlstate(excinfo.value) == "2BP01"  # dependent_objects_still_exist
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0011"
            )
            linked = await connection.scalar(
                text("SELECT idempotency_key FROM transactions WHERE id = :t"),
                {"t": ids["transaction"]},
            )
            assert linked == "key-1"
    finally:
        await engine.dispose()


async def test_0011_keeps_0010_keys_valid_and_unhijackable() -> None:
    # A key committed at 0010 points at its transaction, which names nothing (the column did
    # not exist). The upgrade validates no existing row, so it stays valid; and a new
    # transaction cannot take the key over, because the key does not point back at it.
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_link_legacy")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    ids = {"user": uuid.uuid7(), "account": uuid.uuid7(), "transaction": uuid.uuid7()}
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0010"))
        async with engine.begin() as connection:
            for statement in COMMITTED_KEYED_DEPOSIT:
                await connection.execute(text(statement.format(**ids)))
        before = await _snapshot(engine)

        await run_alembic(engine, lambda config: command.upgrade(config, "head"))

        assert await _snapshot(engine) == before  # legacy rows untouched...
        async with engine.connect() as connection:
            legacy = (
                await connection.execute(
                    text(
                        "SELECT k.transaction_id, t.idempotency_user_id, t.idempotency_key "
                        "FROM idempotency_keys k JOIN transactions t ON t.id = k.transaction_id"
                    )
                )
            ).one()
        assert tuple(legacy) == (ids["transaction"], None, None)  # ...and still unlinked

        # Hijack attempt: a new, balanced transaction naming the legacy key.
        hijack = uuid.uuid7()
        async with engine.connect() as connection:
            await connection.begin()
            await connection.execute(
                text(
                    "INSERT INTO transactions "
                    "(id, ledger_id, kind, entry_count, idempotency_user_id, idempotency_key) "
                    "SELECT :t, id, 'deposit', 2, :u, 'key-1' FROM ledgers WHERE currency = 'GBP'"
                ),
                {"t": hijack, "u": ids["user"]},
            )
            await connection.execute(
                text(
                    "INSERT INTO ledger_entries "
                    "(transaction_id, account_id, ledger_id, amount_minor) "
                    "SELECT :t, a.id, a.ledger_id, CASE a.kind WHEN 'system' THEN -1 ELSE 1 END "
                    "FROM accounts a JOIN ledgers l ON l.id = a.ledger_id "
                    "WHERE l.currency = 'GBP' AND (a.kind = 'system' OR a.id = :a)"
                ),
                {"t": hijack, "a": ids["account"]},
            )
            with pytest.raises(IntegrityError) as excinfo:
                await connection.commit()
        assert violated_constraint(excinfo.value) == "ck_idempotency_keys_transaction_link"
    finally:
        await engine.dispose()


LEGACY_KEYS = text(
    "SELECT user_id, key, request_fingerprint, transaction_id, created_at FROM idempotency_keys"
)


async def _version(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    return version if isinstance(version, str) else None


async def test_downgrade_from_0011_keeps_0010_keys_behind_0010s_guard() -> None:
    # A 0011 database holding only a legacy (0010-era, unlinked) key. Downgrading to 0009:
    # 0011 has no links to protect and downgrades; 0010 then refuses to drop a populated
    # idempotency_keys. Alembic runs one call in one transaction, so a single call from 0011
    # is refused as a whole (still at 0011); step by step, 0011 commits and 0010 refuses.
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_legacy_downgrade")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    ids = {"user": uuid.uuid7(), "account": uuid.uuid7(), "transaction": uuid.uuid7()}
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0010"))
        async with engine.begin() as connection:
            for statement in COMMITTED_KEYED_DEPOSIT:
                await connection.execute(text(statement.format(**ids)))
        # 0011 explicitly: the revision whose downgrade path this exercises (later revisions
        # add their own downgrade guards in front of it).
        await run_alembic(engine, lambda config: command.upgrade(config, "0011"))
        async with engine.connect() as connection:
            legacy = [tuple(row) for row in await connection.execute(LEGACY_KEYS)]
            columns_0011 = list((await connection.execute(TRANSACTION_COLUMNS)).scalars())
        assert [row[3] for row in legacy] == [ids["transaction"]]
        rows = await _snapshot(engine)

        # One call, head -> 0009: refused by 0010's guard (0011's guard found no link).
        with pytest.raises(DBAPIError) as excinfo:
            await run_alembic(engine, lambda config: command.downgrade(config, "0009"))
        assert sqlstate(excinfo.value) == "2BP01"
        assert "refusing to drop idempotency_keys" in str(excinfo.value.orig)
        assert "idempotency links" not in str(excinfo.value.orig)  # not 0011's refusal
        # Atomic: nothing of 0011's downgrade survived the refusal.
        assert await _version(engine) == "0011"
        async with engine.connect() as connection:
            assert list((await connection.execute(TRANSACTION_COLUMNS)).scalars()) == columns_0011
            assert [tuple(row) for row in await connection.execute(LEGACY_KEYS)] == legacy
        assert await _snapshot(engine) == rows

        # Step by step: 0011 downgrades (it has no links to protect)...
        await run_alembic(engine, lambda config: command.downgrade(config, "0010"))
        assert await _version(engine) == "0010"
        async with engine.connect() as connection:
            columns_0010 = set((await connection.execute(TRANSACTION_COLUMNS)).scalars())
            assert [tuple(row) for row in await connection.execute(LEGACY_KEYS)] == legacy
        assert columns_0010 == set(columns_0011) - {"idempotency_user_id", "idempotency_key"}
        assert "assert_idempotency_link" not in await _public_functions(engine)

        # ...and 0010 refuses to drop the populated table, leaving everything in place.
        with pytest.raises(DBAPIError) as excinfo:
            await run_alembic(engine, lambda config: command.downgrade(config, "0009"))
        assert sqlstate(excinfo.value) == "2BP01"
        assert "refusing to drop idempotency_keys" in str(excinfo.value.orig)
        assert await _version(engine) == "0010"
        async with engine.connect() as connection:
            assert [tuple(row) for row in await connection.execute(LEGACY_KEYS)] == legacy
        assert await _snapshot(engine) == rows
        assert "idempotency_keys" in await _public_tables(engine)
    finally:
        await engine.dispose()


# Every row a 0009 database can hold, in a stable order: compared before and after 0010.
SNAPSHOT_0009 = {
    "users": "SELECT id, email, created_at FROM users ORDER BY id",
    "accounts": "SELECT id, user_id, kind, ledger_id, balance_minor FROM accounts ORDER BY id",
    "transactions": "SELECT id, ledger_id, kind, entry_count, created_at FROM transactions "
    "ORDER BY id",
    "ledger_entries": "SELECT id, transaction_id, account_id, amount_minor, sequence_number "
    "FROM ledger_entries ORDER BY id",
    "audit_events": "SELECT id, action, outcome, reason, actor_user_id, idempotency_key, details "
    "FROM audit_events ORDER BY id",
}


async def _snapshot(engine: AsyncEngine) -> dict[str, list[tuple[object, ...]]]:
    async with engine.connect() as connection:
        return {
            table: [tuple(row) for row in await connection.execute(text(query))]
            for table, query in SNAPSHOT_0009.items()
        }


async def test_0010_upgrades_a_populated_0009_database_and_keyed_postings_work() -> None:
    # An existing database, not a fresh one: data written at 0009 survives 0010 unchanged, and
    # the upgraded schema accepts keyed postings and replays through the real services.
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_populated_0009")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    app_engine = create_engine(settings_for(url), privilege_guard=False)  # superuser
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0009"))
        async with engine.begin() as connection:
            await _post_raw_deposit(connection, amount=2_500)
            await connection.execute(
                text(
                    "INSERT INTO audit_events (action, outcome, reason) "
                    "VALUES ('user.register', 'rejected', 'email_already_registered')"
                )
            )
        before = await _snapshot(engine)
        assert {table: len(rows) for table, rows in before.items()} == {
            "users": 1,
            "accounts": 1 + len(Currency),  # the customer and one settlement account per ledger
            "transactions": 1,
            "ledger_entries": 2,
            "audit_events": 1,
        }

        await run_alembic(engine, lambda config: command.upgrade(config, "head"))

        assert await _snapshot(engine) == before  # every pre-existing row, byte for byte
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM idempotency_keys")) == 0
            owner_id, account_id = (
                await connection.execute(
                    text("SELECT user_id, id FROM accounts WHERE kind = 'customer'")
                )
            ).one()

        sessionmaker = create_sessionmaker(app_engine)
        results = []
        for _ in range(2):  # the posting, then its retry with the same key
            async with sessionmaker() as session:
                results.append(
                    await withdraw(
                        session,
                        owner_id=owner_id,
                        account_id=account_id,
                        amount_minor=1_000,
                        idempotency_key="after-0010",
                    )
                )
        posted, replayed = results
        assert (posted.replayed, replayed.replayed) == (False, True)
        assert replayed.transaction_id == posted.transaction_id
        assert posted.balances == replayed.balances == {account_id: 1_500}
        async with sessionmaker() as session:
            key = await session.get(IdempotencyKey, (owner_id, "after-0010"))
            assert key is not None
            assert key.transaction_id == posted.transaction_id
            await assert_reconciled(session)
    finally:
        await app_engine.dispose()
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
    # idempotency=False: the schema under test may predate 0010; raw inserts never use keys.
    for grant in posting_role_grants(
        role, helper=helper, idempotency=False, schema_before_0011=True
    ):
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


BALANCE_CHECK_DEFINITION = text("SELECT pg_get_functiondef('assert_transaction_balanced'::regproc)")
BALANCE_CHECK_CONFIG = text(
    "SELECT proconfig FROM pg_proc WHERE oid = 'assert_transaction_balanced'::regproc"
)


async def _commit_check_unbalanced_behind_shadows(connection: AsyncConnection) -> None:
    """In the caller's (rolled-back) transaction: a role with the posting privileges writes an
    unbalanced transaction, shadows the tables the deferred check reads with a balanced
    version, and forces the check."""
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
        {"email": f"shadow-{uuid.uuid7().hex}@example.com"},
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
    # idempotency=False: the schema under test may predate 0010; raw inserts never use keys.
    for grant in posting_role_grants(role, idempotency=False, schema_before_0011=True):
        await connection.execute(text(grant))
    await connection.execute(text(f'SET LOCAL ROLE "{role}"'))
    transaction_id = await connection.scalar(
        text(
            "INSERT INTO public.transactions (ledger_id, kind, entry_count) "
            "VALUES (:ledger, 'deposit', 2) RETURNING id"
        ),
        {"ledger": ledger_id},
    )
    await connection.execute(
        text(
            "INSERT INTO public.ledger_entries (transaction_id, account_id, ledger_id, "
            "amount_minor) VALUES (:t, :s, :l, -1), (:t, :a, :l, 1000)"
        ),
        {"t": transaction_id, "s": settlement_id, "a": account_id, "l": ledger_id},
    )
    await connection.execute(text("CREATE TEMP TABLE transactions (id uuid, entry_count int)"))
    await connection.execute(
        text("CREATE TEMP TABLE ledger_entries (transaction_id uuid, amount_minor bigint)")
    )
    await connection.execute(
        text("INSERT INTO pg_temp.transactions VALUES (:t, 2)"), {"t": transaction_id}
    )
    await connection.execute(
        text("INSERT INTO pg_temp.ledger_entries VALUES (:t, -1), (:t, 1)"),
        {"t": transaction_id},
    )
    await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


async def test_0009_pins_the_balance_check_search_path_and_downgrades_exactly() -> None:
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_balance_check_search_path")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0008"))
        async with engine.connect() as connection:
            definition_0008 = await connection.scalar(BALANCE_CHECK_DEFINITION)
            assert await connection.scalar(BALANCE_CHECK_CONFIG) is None
            # The vulnerability, reproduced at 0008: the check read the shadows and let an
            # unbalanced transaction (-1, +1000) through.
            await _commit_check_unbalanced_behind_shadows(connection)
            await connection.rollback()

        await run_alembic(engine, lambda config: command.upgrade(config, "head"))
        async with engine.connect() as connection:
            assert await connection.scalar(BALANCE_CHECK_CONFIG) == [
                "search_path=pg_catalog, pg_temp"
            ]
            definition_0009 = await connection.scalar(BALANCE_CHECK_DEFINITION)
            with pytest.raises(IntegrityError) as excinfo:
                await _commit_check_unbalanced_behind_shadows(connection)
            await connection.rollback()
            assert violated_constraint(excinfo.value) == "ck_transactions_balanced"
            await _insert_entries_as_new_role(connection, helper=True)  # still posts normally
            await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            await connection.rollback()

        await run_alembic(engine, lambda config: command.downgrade(config, "0008"))
        async with engine.connect() as connection:
            # 0008's (that is, 0002's) function, restored byte for byte, path unpinned.
            assert await connection.scalar(BALANCE_CHECK_DEFINITION) == definition_0008
            assert await connection.scalar(BALANCE_CHECK_CONFIG) is None

        await run_alembic(engine, lambda config: command.upgrade(config, "head"))
        async with engine.connect() as connection:
            assert await connection.scalar(BALANCE_CHECK_DEFINITION) == definition_0009
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


# --- 0012: authentication -----------------------------------------------------------------------

AUDIT_ACTION_CHECK = text(
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_audit_events_action'"
)
ARGON2ID_SAMPLE = "$argon2id$v=19$m=8,t=1,p=1$c2FsdHNhbHQxMjM0NTY3OA$" + "a" * 43


async def _engine_at(name: str, revision: str) -> AsyncEngine:
    server_url = configured_database_url()
    url = disposable_database_url(server_url, name)
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    await run_alembic(engine, lambda config: command.upgrade(config, revision))
    return engine


async def _new_user(connection: AsyncConnection) -> uuid.UUID:
    user_id = await connection.scalar(
        text("INSERT INTO users (email) VALUES (:email) RETURNING id"),
        {"email": f"auth-{uuid.uuid7().hex}@example.test"},
    )
    assert isinstance(user_id, uuid.UUID)
    return user_id


async def test_0012_round_trips_exactly_on_an_empty_database() -> None:
    engine = await _engine_at("_auth_round_trip", "0011")
    try:
        tables_0011, functions_0011 = await _public_tables(engine), await _public_functions(engine)
        async with engine.connect() as connection:
            actions_0011 = await connection.scalar(AUDIT_ACTION_CHECK)

        await run_alembic(engine, lambda config: command.upgrade(config, "0012"))
        assert await _public_tables(engine) == tables_0011 | {"user_credentials", "auth_sessions"}
        assert await _public_functions(engine) == functions_0011 | {"forbid_auth_session_changes"}
        async with engine.connect() as connection:
            actions_0012 = await connection.scalar(AUDIT_ACTION_CHECK)
        assert isinstance(actions_0012, str)
        assert "auth.login" in actions_0012
        assert "auth.logout" in actions_0012

        await run_alembic(engine, lambda config: command.downgrade(config, "0011"))
        assert await _public_tables(engine) == tables_0011
        assert await _public_functions(engine) == functions_0011
        async with engine.connect() as connection:
            assert await connection.scalar(AUDIT_ACTION_CHECK) == actions_0011  # byte for byte

        await run_alembic(engine, lambda config: command.upgrade(config, "0012"))
        assert await _version(engine) == "0012"
    finally:
        await engine.dispose()


async def test_0012_upgrades_a_populated_0011_database_without_backfilling() -> None:
    engine = await _engine_at("_auth_populated", "0011")
    try:
        async with engine.begin() as connection:
            await _post_raw_deposit(connection, amount=4_200)
            await connection.execute(
                text(
                    "INSERT INTO audit_events (action, outcome, reason) "
                    "VALUES ('user.register', 'rejected', 'email_already_registered')"
                )
            )
        before = await _snapshot(engine)

        await run_alembic(engine, lambda config: command.upgrade(config, "0012"))

        assert await _snapshot(engine) == before  # every existing row unchanged
        async with engine.connect() as connection:
            # No credentials or sessions appear: existing users simply cannot log in.
            assert await connection.scalar(text("SELECT count(*) FROM user_credentials")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM auth_sessions")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM users")) == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("populate", "message"),
    [
        (
            "INSERT INTO user_credentials (user_id, password_hash) VALUES (:user, :hash)",
            "refusing to drop user_credentials",
        ),
        (
            "INSERT INTO audit_events (action, outcome, reason) "
            "VALUES ('auth.login', 'rejected', 'invalid_credentials')",
            "authentication audit events exist",
        ),
    ],
    ids=["credentials", "auth-audit-events"],
)
async def test_0012_downgrade_refuses_to_discard_credentials_or_auth_events(
    populate: str, message: str
) -> None:
    engine = await _engine_at("_auth_guard", "0012")
    try:
        async with engine.begin() as connection:
            user_id = await _new_user(connection)
            await connection.execute(text(populate), {"user": user_id, "hash": ARGON2ID_SAMPLE})
        rows = await _snapshot(engine)

        with pytest.raises(DBAPIError) as excinfo:
            await run_alembic(engine, lambda config: command.downgrade(config, "0011"))

        assert sqlstate(excinfo.value) == "2BP01"  # dependent_objects_still_exist
        assert message in str(excinfo.value.orig)
        assert await _version(engine) == "0012"  # nothing of the downgrade survived
        assert {"user_credentials", "auth_sessions"} <= await _public_tables(engine)
        assert await _snapshot(engine) == rows
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT count(*) FROM user_credentials"))
                + await connection.scalar(
                    text("SELECT count(*) FROM audit_events WHERE action LIKE 'auth.%'")
                )
            ) == 1
    finally:
        await engine.dispose()


async def test_0012_downgrade_drops_sessions_when_nothing_else_is_at_stake() -> None:
    # Sessions alone are not a reason to refuse: dropping them only logs everyone out.
    engine = await _engine_at("_auth_sessions_only", "0012")
    try:
        async with engine.begin() as connection:
            user_id = await _new_user(connection)
            await connection.execute(
                text(
                    "INSERT INTO auth_sessions (user_id, token_hash, expires_at) "
                    "VALUES (:user, decode(repeat('ab', 32), 'hex'), now() + interval '30 minutes')"
                ),
                {"user": user_id},
            )

        await run_alembic(engine, lambda config: command.downgrade(config, "0011"))

        assert await _version(engine) == "0011"
        assert not {"user_credentials", "auth_sessions"} & await _public_tables(engine)
        assert "forbid_auth_session_changes" not in await _public_functions(engine)

        await run_alembic(engine, lambda config: command.upgrade(config, "0012"))
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM auth_sessions")) == 0
    finally:
        await engine.dispose()
