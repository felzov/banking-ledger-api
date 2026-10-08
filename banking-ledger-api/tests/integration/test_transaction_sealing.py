"""Every transaction declares its entry count (migration 0002), so a posted transaction can
never gain entries later, even balanced ones."""

import pytest
from alembic import command
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.errors import violated_constraint
from ledger_api.data.models import Account, LedgerEntry, Transaction
from ledger_api.domain.currency import Currency
from ledger_api.domain.transaction import TransactionKind
from tests.database import (
    check_deferred_constraints,
    configured_database_url,
    disposable_database_url,
    raises_violation,
    recreate_database,
    run_alembic,
)
from tests.factories import (
    add_entries,
    create_customer_account,
    create_transaction,
    get_settlement_account,
)


async def test_entry_count_must_match_the_entries(db_session: AsyncSession) -> None:
    payer = await create_customer_account(db_session, balance_minor=100)
    payee = await create_customer_account(db_session)
    transaction = await create_transaction(db_session, entry_count=3)
    add_entries(db_session, transaction, (payer, -100), (payee, 100))

    async with raises_violation("ck_transactions_entry_count"):
        await check_deferred_constraints(db_session)


async def test_entry_count_below_two_is_rejected(db_session: AsyncSession) -> None:
    async with raises_violation("ck_transactions_entry_count_min"):
        await create_transaction(db_session, entry_count=1)


async def test_balanced_entries_cannot_be_appended_to_a_committed_transaction(
    engine: AsyncEngine,
) -> None:
    # The Phase 2 gap, reproduced with real COMMITs: each check passes on its own (>= 2
    # entries, sum zero), only the declared count catches the append.
    # Committed data stays reconciled: the balances match the posted entries.
    sessionmaker = create_sessionmaker(engine)
    async with sessionmaker() as session, session.begin():
        settlement = await get_settlement_account(session, Currency.GBP)
        customer = await create_customer_account(session)
        # Two accounts not yet in the transaction, so the unique (transaction, account) rule
        # cannot be what rejects the append.
        other = await create_customer_account(session)
        third = await create_customer_account(session)
        posted = await create_transaction(session, kind=TransactionKind.DEPOSIT)
        add_entries(session, posted, (settlement, -100), (customer, 100))
        for account_id, delta in ((settlement.id, -100), (customer.id, 100)):
            await session.execute(
                update(Account)
                .where(Account.id == account_id)
                .values(balance_minor=Account.balance_minor + delta)
            )
    posted_id, other_id, third_id, ledger_id = posted.id, other.id, third.id, posted.ledger_id

    with pytest.raises(IntegrityError) as excinfo:
        async with sessionmaker() as session, session.begin():
            session.add_all(
                [
                    LedgerEntry(
                        transaction_id=posted_id,
                        account_id=other_id,
                        ledger_id=ledger_id,
                        amount_minor=500,
                    ),
                    LedgerEntry(
                        transaction_id=posted_id,
                        account_id=third_id,
                        ledger_id=ledger_id,
                        amount_minor=-500,
                    ),
                ]
            )

    assert violated_constraint(excinfo.value) == "ck_transactions_entry_count"
    async with sessionmaker() as session:
        entries = await session.scalar(
            select(func.count()).where(LedgerEntry.transaction_id == posted_id)
        )
        assert entries == 2
        assert await session.get(Transaction, posted_id) is not None


async def test_migration_backfills_the_count_and_restores_immutability() -> None:
    server_url = configured_database_url()
    url = disposable_database_url(server_url, "_backfill")
    assert url.database is not None
    await recreate_database(server_url, url.database)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await run_alembic(engine, lambda config: command.upgrade(config, "0001"))
        async with engine.begin() as connection:
            # A transaction posted under 0001, i.e. without entry_count.
            ledger_id, settlement_id = (
                await connection.execute(
                    text(
                        "SELECT l.id, a.id FROM ledgers l JOIN accounts a "
                        "ON a.ledger_id = l.id AND a.kind = 'system' WHERE l.currency = 'GBP'"
                    )
                )
            ).one()
            user_id = await connection.scalar(
                text("INSERT INTO users (email) VALUES ('b@example.com') RETURNING id")
            )
            account_id = await connection.scalar(
                text(
                    "INSERT INTO accounts (ledger_id, user_id, kind) "
                    "VALUES (:ledger, :user, 'customer') RETURNING id"
                ),
                {"ledger": ledger_id, "user": user_id},
            )
            transaction_id = await connection.scalar(
                text(
                    "INSERT INTO transactions (ledger_id, kind) "
                    "VALUES (:ledger, 'deposit') RETURNING id"
                ),
                {"ledger": ledger_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO ledger_entries (transaction_id, account_id, ledger_id, "
                    "amount_minor) VALUES (:t, :s, :l, -100), (:t, :a, :l, 100)"
                ),
                {"t": transaction_id, "s": settlement_id, "a": account_id, "l": ledger_id},
            )

        await run_alembic(engine, lambda config: command.upgrade(config, "head"))

        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT entry_count FROM transactions WHERE id = :id"),
                    {"id": transaction_id},
                )
                == 2
            )
            with pytest.raises(IntegrityError) as excinfo:
                await connection.execute(
                    text("UPDATE transactions SET entry_count = 3 WHERE id = :id"),
                    {"id": transaction_id},
                )
            assert violated_constraint(excinfo.value) == "transactions_immutable"
    finally:
        await engine.dispose()
