"""Double-entry invariants (ADR 0003): enforced by deferred triggers and composite foreign keys."""

import uuid

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.errors import violated_constraint
from ledger_api.data.models import Account, LedgerEntry, Transaction
from ledger_api.domain.currency import Currency
from ledger_api.domain.transaction import TransactionKind
from tests.database import check_deferred_constraints, raises_violation
from tests.factories import (
    add_entries,
    create_customer_account,
    create_transaction,
    get_ledger,
    get_settlement_account,
)


async def _entry_sum(session: AsyncSession, transaction: Transaction) -> int | None:
    return await session.scalar(
        select(func.sum(LedgerEntry.amount_minor)).where(
            LedgerEntry.transaction_id == transaction.id
        )
    )


# --- accepted postings -------------------------------------------------------------------------


async def test_balanced_transfer_is_accepted(db_session: AsyncSession) -> None:
    payer = await create_customer_account(db_session, balance_minor=5000)
    payee = await create_customer_account(db_session)
    transaction = await create_transaction(db_session, kind=TransactionKind.TRANSFER)

    add_entries(db_session, transaction, (payer, -3000), (payee, 3000))
    await check_deferred_constraints(db_session)

    assert await _entry_sum(db_session, transaction) == 0


@pytest.mark.parametrize(
    ("kind", "settlement_amount"),
    [(TransactionKind.DEPOSIT, -10_000), (TransactionKind.WITHDRAWAL, 10_000)],
)
async def test_deposits_and_withdrawals_post_against_the_settlement_account(
    db_session: AsyncSession, kind: TransactionKind, settlement_amount: int
) -> None:
    settlement = await get_settlement_account(db_session, Currency.GBP)
    customer = await create_customer_account(db_session, balance_minor=10_000)
    transaction = await create_transaction(db_session, kind=kind)

    add_entries(
        db_session, transaction, (settlement, settlement_amount), (customer, -settlement_amount)
    )
    await check_deferred_constraints(db_session)

    assert await _entry_sum(db_session, transaction) == 0


async def test_balanced_posting_with_more_than_two_entries_is_accepted(
    db_session: AsyncSession,
) -> None:
    # The rule is "sum to zero", not "exactly one debit and one credit".
    payer = await create_customer_account(db_session, balance_minor=100)
    first = await create_customer_account(db_session)
    second = await create_customer_account(db_session)
    transaction = await create_transaction(db_session)

    add_entries(db_session, transaction, (payer, -100), (first, 60), (second, 40))
    await check_deferred_constraints(db_session)

    assert await _entry_sum(db_session, transaction) == 0


async def test_balance_is_checked_at_commit_not_after_each_row(db_session: AsyncSession) -> None:
    payer = await create_customer_account(db_session)
    payee = await create_customer_account(db_session)
    transaction = await create_transaction(db_session)

    # Mid-posting the transaction is unbalanced; a non-deferred check would reject this flush.
    add_entries(db_session, transaction, (payer, -3000))
    await db_session.flush()

    add_entries(db_session, transaction, (payee, 3000))
    await check_deferred_constraints(db_session)


# --- rejected postings -------------------------------------------------------------------------


async def test_unbalanced_transaction_is_rejected(db_session: AsyncSession) -> None:
    payer = await create_customer_account(db_session)
    payee = await create_customer_account(db_session)
    transaction = await create_transaction(db_session)
    add_entries(db_session, transaction, (payer, -3000), (payee, 2999))

    async with raises_violation("ck_transactions_balanced"):
        await check_deferred_constraints(db_session)


async def test_transaction_with_a_single_entry_is_rejected(db_session: AsyncSession) -> None:
    account = await create_customer_account(db_session)
    transaction = await create_transaction(db_session)
    add_entries(db_session, transaction, (account, 3000))

    async with raises_violation("ck_transactions_min_two_entries"):
        await check_deferred_constraints(db_session)


async def test_transaction_without_entries_is_rejected(db_session: AsyncSession) -> None:
    # Only the trigger on transactions can catch this: no entry row exists to fire the other.
    await create_transaction(db_session)

    async with raises_violation("ck_transactions_min_two_entries"):
        await check_deferred_constraints(db_session)


async def test_unbalancing_entry_appended_to_a_checked_transaction_is_rejected(
    db_session: AsyncSession,
) -> None:
    # Once the transaction's own trigger has run, only the trigger on ledger_entries can catch
    # entries appended afterwards. (Balanced appends are a known gap: the posting
    # finalization invariant is designed in Phase 4/5.)
    payer = await create_customer_account(db_session, balance_minor=5000)
    payee = await create_customer_account(db_session)
    intruder = await create_customer_account(db_session)
    transaction = await create_transaction(db_session)
    add_entries(db_session, transaction, (payer, -3000), (payee, 3000))
    await check_deferred_constraints(db_session)

    add_entries(db_session, transaction, (intruder, 500))

    async with raises_violation("ck_transactions_balanced"):
        await check_deferred_constraints(db_session)


async def _commit_single_entry_deposit(
    sessionmaker: async_sessionmaker[AsyncSession], transaction_id: uuid.UUID
) -> None:
    async with sessionmaker() as session, session.begin():
        ledger = await get_ledger(session, Currency.GBP)
        settlement = await get_settlement_account(session, Currency.GBP)
        session.add(
            Transaction(id=transaction_id, ledger_id=ledger.id, kind=TransactionKind.DEPOSIT)
        )
        await session.flush()
        session.add(
            LedgerEntry(
                transaction_id=transaction_id,
                account_id=settlement.id,
                ledger_id=ledger.id,
                amount_minor=-10_000,
            )
        )


async def test_invalid_posting_fails_at_a_real_commit_and_leaves_nothing(
    engine: AsyncEngine,
) -> None:
    # The other tests force deferred checks with SET CONSTRAINTS; this one commits for real.
    sessionmaker = create_sessionmaker(engine)
    transaction_id = uuid.uuid7()

    with pytest.raises(IntegrityError) as excinfo:
        await _commit_single_entry_deposit(sessionmaker, transaction_id)

    assert violated_constraint(excinfo.value) == "ck_transactions_min_two_entries"
    async with sessionmaker() as session:
        assert await session.get(Transaction, transaction_id) is None
        remaining_entries = await session.scalar(
            select(func.count()).where(LedgerEntry.transaction_id == transaction_id)
        )
        assert remaining_entries == 0


async def test_entry_amount_cannot_be_zero(db_session: AsyncSession) -> None:
    account = await create_customer_account(db_session)
    transaction = await create_transaction(db_session)
    add_entries(db_session, transaction, (account, 0))

    async with raises_violation("ck_ledger_entries_amount_nonzero"):
        await db_session.flush()


async def test_account_cannot_appear_twice_in_one_transaction(db_session: AsyncSession) -> None:
    # Sums to zero, but an A -> A "transfer" is meaningless.
    account = await create_customer_account(db_session, balance_minor=3000)
    transaction = await create_transaction(db_session)
    add_entries(db_session, transaction, (account, -3000), (account, 3000))

    async with raises_violation("uq_ledger_entries_transaction_id_account_id"):
        await db_session.flush()


# --- cross-currency postings are unrepresentable -----------------------------------------------


async def test_entry_cannot_post_to_an_account_in_another_ledger(db_session: AsyncSession) -> None:
    eur_account = await create_customer_account(db_session, Currency.EUR)
    gbp_transaction = await create_transaction(db_session, Currency.GBP)
    # The entry takes the transaction's (GBP) ledger; the account lives in the EUR ledger.
    add_entries(db_session, gbp_transaction, (eur_account, 3000))

    async with raises_violation("fk_ledger_entries_account_id_ledger_id_accounts"):
        await db_session.flush()


async def test_entry_cannot_claim_a_ledger_other_than_its_transactions(
    db_session: AsyncSession,
) -> None:
    eur_account = await create_customer_account(db_session, Currency.EUR)
    gbp_transaction = await create_transaction(db_session, Currency.GBP)
    # Consistent with the account, inconsistent with the transaction.
    db_session.add(
        LedgerEntry(
            transaction_id=gbp_transaction.id,
            account_id=eur_account.id,
            ledger_id=eur_account.ledger_id,
            amount_minor=3000,
        )
    )

    async with raises_violation("fk_ledger_entries_transaction_id_ledger_id_transactions"):
        await db_session.flush()


async def test_transaction_kind_must_be_known(db_session: AsyncSession) -> None:
    ledger = await get_ledger(db_session, Currency.GBP)

    async with raises_violation("ck_transactions_kind"):
        await db_session.execute(insert(Transaction).values(ledger_id=ledger.id, kind="refund"))


async def test_every_transaction_kind_is_accepted_by_the_database(
    db_session: AsyncSession,
) -> None:
    # Guards against the Python enum and the CHECK constraint drifting apart.
    payer = await create_customer_account(db_session, balance_minor=3 * 100)
    payees = [await create_customer_account(db_session) for _ in TransactionKind]

    for kind, payee in zip(TransactionKind, payees, strict=True):
        transaction = await create_transaction(db_session, kind=kind)
        add_entries(db_session, transaction, (payer, -100), (payee, 100))
    await check_deferred_constraints(db_session)

    stored = await db_session.scalars(select(Transaction.kind).distinct())
    assert set(stored) >= set(TransactionKind)


async def test_accounts_table_is_untouched_by_entries(db_session: AsyncSession) -> None:
    # balance_minor is an application-maintained projection (ADR 0003): posting entries must
    # not change it behind the service's back (no balance-updating trigger exists).
    payer = await create_customer_account(db_session, balance_minor=5000)
    payee = await create_customer_account(db_session)
    transaction = await create_transaction(db_session)
    add_entries(db_session, transaction, (payer, -3000), (payee, 3000))
    await check_deferred_constraints(db_session)

    balances = await db_session.scalars(
        select(Account.balance_minor).where(Account.id.in_([payer.id, payee.id]))
    )
    assert sorted(balances) == [0, 5000]
