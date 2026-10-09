"""Reconciliation detects inconsistencies without trusting the triggers it double-checks.

Every corruption here is planted inside the test's rolled-back transaction on the disposable
test database; nothing is ever committed, and the development database is never touched.
Where the schema itself would refuse the corruption (foreign keys), bypass_triggers() plays a
superuser who disabled the protection, which is exactly what reconciliation exists to catch.
"""

import uuid

from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import Account, AuditEvent, LedgerEntry
from ledger_api.data.reconciliation import (
    InvalidTransaction,
    find_balance_mismatches,
    find_failed_events_for_committed_transactions,
    find_invalid_transactions,
    find_ledgers_with_nonzero_balances,
    find_misplaced_entries,
    find_posted_transactions_without_success_audit,
    find_unbalanced_ledgers,
)
from ledger_api.domain.currency import Currency
from ledger_api.domain.transaction import TransactionKind
from ledger_api.services.posting import deposit, transfer
from tests.conftest import SessionFactory
from tests.database import assert_reconciled, bypass_triggers, check_deferred_constraints
from tests.factories import add_entries, create_customer_account, create_transaction, create_user


def _owner(account: Account) -> uuid.UUID:
    assert account.user_id is not None
    return account.user_id


# --- a clean ledger ----------------------------------------------------------------------------


async def test_postings_through_the_services_reconcile_completely(
    db_session: AsyncSession, new_session: SessionFactory
) -> None:
    payer = await create_customer_account(db_session)
    payee = await create_customer_account(db_session)
    funding = await deposit(
        new_session(), owner_id=_owner(payer), account_id=payer.id, amount_minor=500
    )
    moved = await transfer(
        new_session(),
        owner_id=_owner(payer),
        source_account_id=payer.id,
        destination_account_id=payee.id,
        amount_minor=200,
    )

    await assert_reconciled(db_session)
    posted = [funding.transaction_id, moved.transaction_id]
    assert (
        await find_posted_transactions_without_success_audit(db_session, transaction_ids=posted)
        == []
    )


# --- balances ----------------------------------------------------------------------------------


async def test_balance_that_does_not_match_its_entries_is_reported(
    db_session: AsyncSession,
) -> None:
    account = await create_customer_account(db_session)
    await db_session.execute(
        update(Account).where(Account.id == account.id).values(balance_minor=999)
    )

    mismatches = await find_balance_mismatches(db_session)

    assert [(m.account_id, m.balance_minor, m.entries_total_minor) for m in mismatches] == [
        (account.id, 999, 0)
    ]
    assert account.ledger_id in await find_ledgers_with_nonzero_balances(db_session)


async def test_balance_check_can_be_scoped_to_accounts(db_session: AsyncSession) -> None:
    broken = await create_customer_account(db_session)
    healthy = await create_customer_account(db_session)
    await db_session.execute(update(Account).where(Account.id == broken.id).values(balance_minor=1))

    assert await find_balance_mismatches(db_session, account_ids=[healthy.id]) == []
    assert len(await find_balance_mismatches(db_session, account_ids=[broken.id])) == 1


# --- transactions ------------------------------------------------------------------------------
# The deferred trigger only runs at COMMIT, and these tests never commit: the invalid rows exist
# inside the transaction exactly as if the trigger had been bypassed.


async def test_unbalanced_transaction_is_reported(db_session: AsyncSession) -> None:
    payer = await create_customer_account(db_session, balance_minor=100)
    payee = await create_customer_account(db_session)
    transaction = await create_transaction(db_session)
    add_entries(db_session, transaction, (payer, -100), (payee, 99))
    await db_session.flush()

    assert InvalidTransaction(transaction.id, 2, 2, -1) in await find_invalid_transactions(
        db_session
    )
    assert transaction.ledger_id in await find_unbalanced_ledgers(db_session)


async def test_transaction_with_too_few_entries_is_reported(db_session: AsyncSession) -> None:
    lonely = await create_transaction(db_session)  # no entries at all
    single = await create_transaction(db_session)
    add_entries(db_session, single, (await create_customer_account(db_session), 50))
    await db_session.flush()

    invalid = await find_invalid_transactions(db_session)

    assert InvalidTransaction(lonely.id, 2, 0, 0) in invalid
    assert InvalidTransaction(single.id, 2, 1, 50) in invalid


async def test_entry_count_other_than_declared_is_reported(db_session: AsyncSession) -> None:
    # Balanced, but not what the sealed header declares (e.g. an appended pair).
    payer = await create_customer_account(db_session, balance_minor=100)
    payee = await create_customer_account(db_session)
    transaction = await create_transaction(db_session, entry_count=3)
    add_entries(db_session, transaction, (payer, -100), (payee, 100))
    await db_session.flush()

    assert InvalidTransaction(transaction.id, 3, 2, 0) in await find_invalid_transactions(
        db_session
    )


async def test_valid_transaction_is_not_reported(db_session: AsyncSession) -> None:
    payer = await create_customer_account(db_session, balance_minor=100)
    payee = await create_customer_account(db_session)
    transaction = await create_transaction(db_session)
    add_entries(db_session, transaction, (payer, -100), (payee, 100))
    await check_deferred_constraints(db_session)

    invalid = await find_invalid_transactions(db_session)

    assert transaction.id not in {item.transaction_id for item in invalid}


# --- entries (foreign keys bypassed) -----------------------------------------------------------


async def test_cross_ledger_entry_is_reported(db_session: AsyncSession) -> None:
    eur_account = await create_customer_account(db_session, Currency.EUR)
    gbp_transaction = await create_transaction(db_session, Currency.GBP)
    await bypass_triggers(db_session)  # the composite foreign keys are triggers too
    entry_id = await db_session.scalar(
        insert(LedgerEntry)
        .values(
            transaction_id=gbp_transaction.id,
            account_id=eur_account.id,
            ledger_id=gbp_transaction.ledger_id,
            amount_minor=10,
        )
        .returning(LedgerEntry.id)
    )

    assert entry_id in await find_misplaced_entries(db_session)


async def test_entry_of_a_missing_account_is_reported(db_session: AsyncSession) -> None:
    transaction = await create_transaction(db_session)
    await bypass_triggers(db_session)
    entry_id = await db_session.scalar(
        insert(LedgerEntry)
        .values(
            transaction_id=transaction.id,
            account_id=uuid.uuid7(),
            ledger_id=transaction.ledger_id,
            amount_minor=10,
        )
        .returning(LedgerEntry.id)
    )

    assert entry_id in await find_misplaced_entries(db_session)


# --- audit coverage ----------------------------------------------------------------------------


async def _posted_without_audit(
    session: AsyncSession, kind: TransactionKind = TransactionKind.TRANSFER
) -> uuid.UUID:
    payer = await create_customer_account(session, balance_minor=100)
    payee = await create_customer_account(session)
    transaction = await create_transaction(session, kind=kind)
    add_entries(session, transaction, (payer, -100), (payee, 100))
    await check_deferred_constraints(session)
    return transaction.id


async def test_transaction_without_its_success_event_is_reported(db_session: AsyncSession) -> None:
    transaction_id = await _posted_without_audit(db_session)

    assert await find_posted_transactions_without_success_audit(
        db_session, transaction_ids=[transaction_id]
    ) == [transaction_id]


async def test_success_event_for_another_kind_of_posting_does_not_count(
    db_session: AsyncSession,
) -> None:
    transaction_id = await _posted_without_audit(db_session, TransactionKind.TRANSFER)
    user = await create_user(db_session)
    await db_session.execute(
        insert(AuditEvent).values(
            action="posting.deposit",  # but the transaction is a transfer
            outcome="succeeded",
            actor_user_id=user.id,
            transaction_id=transaction_id,
        )
    )

    assert await find_posted_transactions_without_success_audit(
        db_session, transaction_ids=[transaction_id]
    ) == [transaction_id]


async def test_failed_event_whose_transaction_committed_is_reported(
    db_session: AsyncSession,
) -> None:
    # The caller was told the posting failed (e.g. the connection dropped during COMMIT), yet
    # the transaction exists: the "lost acknowledgement" case.
    committed = await _posted_without_audit(db_session)

    async def failed_event(attempted: uuid.UUID) -> int:
        event_id = await db_session.scalar(
            insert(AuditEvent)
            .values(
                action="posting.withdrawal",
                outcome="failed",
                reason="internal_error",
                details={"attempted_transaction_id": str(attempted)},
            )
            .returning(AuditEvent.id)
        )
        assert isinstance(event_id, int)
        return event_id

    lost_ack = await failed_event(committed)
    genuinely_failed = await failed_event(uuid.uuid7())

    reported = await find_failed_events_for_committed_transactions(db_session)

    assert lost_ack in reported
    assert genuinely_failed not in reported
