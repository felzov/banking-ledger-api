"""Account statements (ADR 0012): order, running balances, keyset pagination, ownership and the
fail-closed self-check. Rolled-back test connection; postings go through the services."""

import dataclasses
import logging
import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import Account, User
from ledger_api.domain.currency import Currency
from ledger_api.domain.errors import (
    AccountNotFoundError,
    InvalidStatementCursorError,
    InvalidStatementLimitError,
    StatementInconsistencyError,
)
from ledger_api.domain.transaction import TransactionKind
from ledger_api.services.posting import PostingResult, deposit, transfer, withdraw
from ledger_api.services.statements import (
    MAX_LIMIT,
    StatementEntry,
    StatementPage,
    get_statement,
)
from tests.conftest import SessionFactory
from tests.factories import create_customer_account, get_settlement_account


async def _account(db_session: AsyncSession, owner: User | None = None) -> Account:
    return await create_customer_account(db_session, Currency.GBP, user=owner)


def _owner(account: Account) -> uuid.UUID:
    assert account.user_id is not None
    return account.user_id


async def _deposit(new_session: SessionFactory, account: Account, amount: int) -> PostingResult:
    return await deposit(
        new_session(), owner_id=_owner(account), account_id=account.id, amount_minor=amount
    )


async def _statement(
    new_session: SessionFactory, account: Account, **kwargs: object
) -> StatementPage:
    return await get_statement(
        new_session(),
        owner_id=_owner(account),
        account_id=account.id,
        **kwargs,  # type: ignore[arg-type]
    )


async def _all_pages(
    new_session: SessionFactory, account: Account, limit: int
) -> list[StatementPage]:
    pages = [await _statement(new_session, account, limit=limit)]
    while pages[-1].next_cursor is not None:
        pages.append(
            await _statement(new_session, account, limit=limit, cursor=pages[-1].next_cursor)
        )
    return pages


# --- content and order -------------------------------------------------------------------------


async def test_account_without_entries_has_an_empty_statement(
    db_session: AsyncSession, new_session: SessionFactory
) -> None:
    account = await _account(db_session)

    page = await _statement(new_session, account)

    assert page == StatementPage(account.id, 0, (), None)


async def test_statement_lists_entries_newest_first_with_running_balances(
    db_session: AsyncSession, new_session: SessionFactory
) -> None:
    account = await _account(db_session)
    other = await _account(db_session)
    owner = _owner(account)
    funding = await _deposit(new_session, account, 1_000)
    cash_out = await withdraw(
        new_session(), owner_id=owner, account_id=account.id, amount_minor=300
    )
    await _deposit(new_session, other, 500)
    incoming = await transfer(
        new_session(),
        owner_id=_owner(other),
        source_account_id=other.id,
        destination_account_id=account.id,
        amount_minor=200,
    )
    outgoing = await transfer(
        new_session(),
        owner_id=owner,
        source_account_id=account.id,
        destination_account_id=other.id,
        amount_minor=100,
    )

    page = await _statement(new_session, account)

    assert page.balance_minor == 800
    assert page.next_cursor is None
    assert [
        (entry.transaction_id, entry.kind, entry.amount_minor, entry.balance_after_minor)
        for entry in page.entries
    ] == [
        (outgoing.transaction_id, TransactionKind.TRANSFER, -100, 800),
        (incoming.transaction_id, TransactionKind.TRANSFER, 200, 900),
        (cash_out.transaction_id, TransactionKind.WITHDRAWAL, -300, 700),
        (funding.transaction_id, TransactionKind.DEPOSIT, 1_000, 1_000),
    ]
    assert page.entries[0].created_at == outgoing.created_at


def test_statement_entries_expose_no_counterparty() -> None:
    assert {field.name for field in dataclasses.fields(StatementEntry)} == {
        "entry_id",
        "transaction_id",
        "kind",
        "amount_minor",
        "balance_after_minor",
        "created_at",
    }


# --- pagination --------------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 2, 3, 7, MAX_LIMIT])
async def test_pages_cover_the_history_exactly_once(
    db_session: AsyncSession, new_session: SessionFactory, limit: int
) -> None:
    account = await _account(db_session)
    for amount in range(1, 8):
        await _deposit(new_session, account, amount)
    full = await _statement(new_session, account, limit=MAX_LIMIT)

    pages = await _all_pages(new_session, account, limit)

    entries = [entry for page in pages for entry in page.entries]
    assert entries == list(full.entries)  # same rows, same order, same running balances
    assert len({entry.entry_id for entry in entries}) == 7  # no duplicates
    assert all(len(page.entries) == limit for page in pages[:-1])
    assert 1 <= len(pages[-1].entries) <= limit
    assert pages[-1].next_cursor is None


async def test_postings_between_page_requests_never_shift_later_pages(
    db_session: AsyncSession, new_session: SessionFactory
) -> None:
    account = await _account(db_session)
    for amount in (10, 20, 30, 40):
        await _deposit(new_session, account, amount)
    first = await _statement(new_session, account, limit=2)

    newer = await _deposit(new_session, account, 50)  # committed between the two requests
    second = await _statement(new_session, account, limit=2, cursor=first.next_cursor)

    assert [entry.amount_minor for entry in first.entries] == [40, 30]
    assert [entry.amount_minor for entry in second.entries] == [20, 10]
    assert [entry.balance_after_minor for entry in second.entries] == [30, 10]  # unchanged
    assert second.next_cursor is None
    fresh = await _statement(new_session, account, limit=2)
    assert fresh.entries[0].transaction_id == newer.transaction_id  # only on a new first page
    assert fresh.balance_minor == 150


@pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1, True])
async def test_page_size_is_bounded(
    db_session: AsyncSession, new_session: SessionFactory, limit: int
) -> None:
    account = await _account(db_session)

    with pytest.raises(InvalidStatementLimitError):
        await _statement(new_session, account, limit=limit)


async def test_cursor_must_be_an_entry_of_the_same_account(
    db_session: AsyncSession, new_session: SessionFactory
) -> None:
    account = await _account(db_session)
    other = await _account(db_session)
    await _deposit(new_session, account, 10)
    await _deposit(new_session, other, 10)
    other_entry = (await _statement(new_session, other)).entries[0].entry_id

    for cursor in (other_entry, uuid.uuid7()):
        with pytest.raises(InvalidStatementCursorError):
            await _statement(new_session, account, cursor=cursor)


# --- ownership ---------------------------------------------------------------------------------


async def test_statement_of_someone_elses_account_is_not_found(
    db_session: AsyncSession, new_session: SessionFactory
) -> None:
    account = await _account(db_session)
    intruder = await _account(db_session)

    with pytest.raises(AccountNotFoundError):
        await get_statement(new_session(), owner_id=_owner(intruder), account_id=account.id)


@pytest.mark.parametrize("target", ["missing", "settlement"])
async def test_statement_of_a_missing_or_system_account_is_not_found(
    db_session: AsyncSession, new_session: SessionFactory, target: str
) -> None:
    account = await _account(db_session)
    settlement = await get_settlement_account(db_session, Currency.GBP)
    account_id = uuid.uuid7() if target == "missing" else settlement.id

    with pytest.raises(AccountNotFoundError):
        await get_statement(new_session(), owner_id=_owner(account), account_id=account_id)


# --- self-check --------------------------------------------------------------------------------


async def test_inconsistent_account_fails_closed_and_logs(
    db_session: AsyncSession, new_session: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    # Deliberate corruption on the rolled-back test connection: the balance no longer equals
    # the entries. The statement must refuse rather than show either number as the truth.
    account = await _account(db_session)
    await _deposit(new_session, account, 100)
    await db_session.execute(
        update(Account).where(Account.id == account.id).values(balance_minor=99)
    )

    with (
        caplog.at_level(logging.ERROR, logger="ledger_api.services.statements"),
        pytest.raises(StatementInconsistencyError),
    ):
        await _statement(new_session, account)

    [log] = caplog.records
    assert str(account.id) in log.getMessage()
    assert "balance_minor=99" in log.getMessage()
