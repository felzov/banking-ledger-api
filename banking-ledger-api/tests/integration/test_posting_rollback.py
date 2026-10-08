"""A posting either commits completely or leaves no trace (real COMMITs, injected failures).

Each test funds an account, then makes a withdrawal fail at one specific point of the
posting: after the header, after the entries, during the balance updates, or at COMMIT.
Afterwards there must be no new transaction, no new entries, unchanged balances, and a
reconciled database.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger_api.data import posting as data_posting
from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.errors import violated_constraint
from ledger_api.data.models import Account, LedgerEntry, Transaction
from ledger_api.domain.currency import Currency
from ledger_api.services import posting as posting_service
from ledger_api.services.accounts import open_customer_account
from ledger_api.services.posting import deposit, withdraw
from ledger_api.services.users import register_user
from tests.database import assert_reconciled
from tests.factories import get_settlement_account

FUNDED = 5_000
WITHDRAWAL = 1_000


class InjectedFailureError(Exception):
    pass


@dataclass(frozen=True)
class Funded:
    owner_id: uuid.UUID
    account_id: uuid.UUID
    settlement_id: uuid.UUID


@dataclass(frozen=True)
class Snapshot:
    transactions: int
    account_entries: int
    balances: tuple[int, int]


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
async def funded(sessionmaker: async_sessionmaker[AsyncSession]) -> Funded:
    async with sessionmaker() as session:
        user = await register_user(session, email=f"rb-{uuid.uuid7().hex}@example.com")
    async with sessionmaker() as session:
        account = await open_customer_account(session, owner_id=user.id, currency=Currency.GBP)
    async with sessionmaker() as session:
        await deposit(session, owner_id=user.id, account_id=account.id, amount_minor=FUNDED)
    async with sessionmaker() as session:
        settlement = await get_settlement_account(session, Currency.GBP)
    return Funded(user.id, account.id, settlement.id)


async def _snapshot(sessionmaker: async_sessionmaker[AsyncSession], funded: Funded) -> Snapshot:
    async with sessionmaker() as session:
        transactions = await session.scalar(select(func.count()).select_from(Transaction))
        entries = await session.scalar(
            select(func.count()).where(LedgerEntry.account_id == funded.account_id)
        )
        rows = await session.execute(
            select(Account.id, Account.balance_minor).where(
                Account.id.in_([funded.account_id, funded.settlement_id])
            )
        )
        balances = {account_id: balance for account_id, balance in rows}
        await assert_reconciled(session)
    assert transactions is not None
    assert entries is not None
    return Snapshot(
        transactions, entries, (balances[funded.account_id], balances[funded.settlement_id])
    )


async def _withdraw(sessionmaker: async_sessionmaker[AsyncSession], funded: Funded) -> None:
    async with sessionmaker() as session:
        await withdraw(
            session, owner_id=funded.owner_id, account_id=funded.account_id, amount_minor=WITHDRAWAL
        )


async def test_successful_withdrawal_is_committed_completely(
    sessionmaker: async_sessionmaker[AsyncSession], funded: Funded
) -> None:
    # Control case: without an injected failure the same operation commits.
    before = await _snapshot(sessionmaker, funded)

    await _withdraw(sessionmaker, funded)

    after = await _snapshot(sessionmaker, funded)
    assert after.transactions == before.transactions + 1
    assert after.account_entries == before.account_entries + 1
    assert after.balances == (FUNDED - WITHDRAWAL, before.balances[1] + WITHDRAWAL)


async def test_failure_after_the_header_insert_leaves_no_trace(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: Funded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_entry_insert(*args: Any, **kwargs: Any) -> Any:
        raise InjectedFailureError  # the header has been flushed; the entries never are

    before = await _snapshot(sessionmaker, funded)
    monkeypatch.setattr(data_posting, "insert", fail_entry_insert)

    with pytest.raises(InjectedFailureError):
        await _withdraw(sessionmaker, funded)

    assert await _snapshot(sessionmaker, funded) == before


async def test_failure_after_the_entries_leaves_no_trace(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: Funded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_balance_update(*args: Any, **kwargs: Any) -> dict[uuid.UUID, int]:
        raise InjectedFailureError  # header and entries are written; balances never are

    before = await _snapshot(sessionmaker, funded)
    monkeypatch.setattr(posting_service, "apply_balance_deltas", fail_balance_update)

    with pytest.raises(InjectedFailureError):
        await _withdraw(sessionmaker, funded)

    assert await _snapshot(sessionmaker, funded) == before


async def test_failure_midway_through_the_balance_updates_leaves_no_trace(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: Funded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulates a posting path that skipped the funds check: the customer delta would
    # overdraw the account. The CHECK constraint stops it mid-way, after the settlement
    # row may already have been updated; that update must be rolled back too.
    real_apply: Callable[..., Awaitable[dict[uuid.UUID, int]]] = data_posting.apply_balance_deltas

    async def overdraw(
        session: AsyncSession, deltas: Mapping[uuid.UUID, int]
    ) -> dict[uuid.UUID, int]:
        tampered = dict(deltas)
        tampered[funded.account_id] = -(FUNDED + 1)
        return await real_apply(session, tampered)

    before = await _snapshot(sessionmaker, funded)
    monkeypatch.setattr(posting_service, "apply_balance_deltas", overdraw)

    with pytest.raises(IntegrityError) as excinfo:
        await _withdraw(sessionmaker, funded)

    assert violated_constraint(excinfo.value) == "ck_accounts_customer_balance_nonnegative"
    assert await _snapshot(sessionmaker, funded) == before


async def test_deferred_failure_at_commit_rolls_back_the_balance_updates(
    sessionmaker: async_sessionmaker[AsyncSession],
    funded: Funded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The header declares one entry too many, so only the COMMIT-time trigger can object:
    # by then the entries are written and both balances already updated.
    def miscounted(**values: Any) -> Transaction:
        return Transaction(**{**values, "entry_count": values["entry_count"] + 1})

    before = await _snapshot(sessionmaker, funded)
    monkeypatch.setattr(data_posting, "Transaction", miscounted)

    with pytest.raises(IntegrityError) as excinfo:
        await _withdraw(sessionmaker, funded)

    assert violated_constraint(excinfo.value) == "ck_transactions_entry_count"
    assert await _snapshot(sessionmaker, funded) == before
