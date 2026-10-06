"""Single-row and referential rules on users, ledgers and accounts."""

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.models import Account, Ledger, User
from ledger_api.domain.account import AccountKind
from ledger_api.domain.currency import Currency
from ledger_api.domain.transaction import TransactionKind
from tests.database import check_deferred_constraints, raises_violation
from tests.factories import (
    add_entries,
    create_customer_account,
    create_transaction,
    create_user,
    get_ledger,
    get_settlement_account,
)

# --- users -------------------------------------------------------------------------------------


async def test_user_email_must_be_lowercase(db_session: AsyncSession) -> None:
    async with raises_violation("ck_users_email_lowercase"):
        await create_user(db_session, "Alice@Example.test")


async def test_user_email_is_unique(db_session: AsyncSession) -> None:
    await create_user(db_session, "alice@example.test")

    async with raises_violation("uq_users_email"):
        await create_user(db_session, "alice@example.test")


async def test_user_email_has_a_minimum_length(db_session: AsyncSession) -> None:
    async with raises_violation("ck_users_email_length"):
        await create_user(db_session, "a@")


# --- ledgers -----------------------------------------------------------------------------------


async def test_ledger_currency_must_be_supported(db_session: AsyncSession) -> None:
    async with raises_violation("ck_ledgers_currency_supported"):
        await db_session.execute(text("INSERT INTO ledgers (currency) VALUES ('USD')"))


async def test_only_one_ledger_per_currency(db_session: AsyncSession) -> None:
    async with raises_violation("uq_ledgers_currency"):
        await db_session.execute(text("INSERT INTO ledgers (currency) VALUES ('GBP')"))


# --- accounts ----------------------------------------------------------------------------------


async def test_one_account_per_user_per_currency(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    await create_customer_account(db_session, Currency.GBP, user=user)

    async with raises_violation("uq_accounts_user_id_ledger_id"):
        await create_customer_account(db_session, Currency.GBP, user=user)


async def test_user_may_hold_one_account_in_each_currency(db_session: AsyncSession) -> None:
    user = await create_user(db_session)

    gbp = await create_customer_account(db_session, Currency.GBP, user=user)
    eur = await create_customer_account(db_session, Currency.EUR, user=user)

    assert gbp.ledger_id != eur.ledger_id


async def test_customer_account_requires_an_owner(db_session: AsyncSession) -> None:
    ledger = await get_ledger(db_session, Currency.GBP)
    db_session.add(Account(ledger_id=ledger.id, user_id=None, kind=AccountKind.CUSTOMER))

    async with raises_violation("ck_accounts_owner_matches_kind"):
        await db_session.flush()


async def test_system_account_cannot_have_an_owner(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    ledger = await get_ledger(db_session, Currency.GBP)
    db_session.add(Account(ledger_id=ledger.id, user_id=user.id, kind=AccountKind.SYSTEM))

    async with raises_violation("ck_accounts_owner_matches_kind"):
        await db_session.flush()


async def test_each_ledger_has_only_one_settlement_account(db_session: AsyncSession) -> None:
    ledger = await get_ledger(db_session, Currency.GBP)
    db_session.add(Account(ledger_id=ledger.id, user_id=None, kind=AccountKind.SYSTEM))

    async with raises_violation("uq_accounts_one_system_account_per_ledger"):
        await db_session.flush()


async def test_account_kind_must_be_known(db_session: AsyncSession) -> None:
    ledger = await get_ledger(db_session, Currency.GBP)

    async with raises_violation("ck_accounts_kind"):
        await db_session.execute(
            text("INSERT INTO accounts (ledger_id, kind) VALUES (:ledger_id, 'savings')"),
            {"ledger_id": ledger.id},
        )


async def test_new_account_starts_with_zero_balance(db_session: AsyncSession) -> None:
    account = await create_customer_account(db_session)

    await db_session.refresh(account)
    assert account.balance_minor == 0


async def test_customer_balance_cannot_go_negative(db_session: AsyncSession) -> None:
    account = await create_customer_account(db_session, balance_minor=500)

    # The same atomic relative update the posting service will use (ADR 0005).
    async with raises_violation("ck_accounts_customer_balance_nonnegative"):
        await db_session.execute(
            update(Account)
            .where(Account.id == account.id)
            .values(balance_minor=Account.balance_minor - 501)
        )


async def test_customer_balance_may_reach_exactly_zero(db_session: AsyncSession) -> None:
    account = await create_customer_account(db_session, balance_minor=500)

    await db_session.execute(
        update(Account)
        .where(Account.id == account.id)
        .values(balance_minor=Account.balance_minor - 500)
    )

    balance = await db_session.scalar(select(Account.balance_minor).where(Account.id == account.id))
    assert balance == 0


async def test_settlement_account_may_go_negative(db_session: AsyncSession) -> None:
    # It mirrors all customer money in its currency, so it is negative by design (ADR 0003).
    settlement = await get_settlement_account(db_session, Currency.GBP)

    await db_session.execute(
        update(Account).where(Account.id == settlement.id).values(balance_minor=-1_000_000)
    )

    balance = await db_session.scalar(
        select(Account.balance_minor).where(Account.id == settlement.id)
    )
    assert balance == -1_000_000


async def test_balance_holds_values_beyond_32_bit_range(db_session: AsyncSession) -> None:
    beyond_int32 = 2**31 * 10  # ~£214M in pence; a 32-bit INTEGER column would overflow.
    account = await create_customer_account(db_session, balance_minor=beyond_int32)

    await db_session.refresh(account)
    assert account.balance_minor == beyond_int32


# --- referential integrity: nothing financial can be deleted out from under the ledger ---------


async def test_user_with_an_account_cannot_be_deleted(db_session: AsyncSession) -> None:
    account = await create_customer_account(db_session)

    async with raises_violation("fk_accounts_user_id_users"):
        await db_session.execute(delete(User).where(User.id == account.user_id))


async def test_ledger_with_accounts_cannot_be_deleted(db_session: AsyncSession) -> None:
    ledger = await get_ledger(db_session, Currency.GBP)

    async with raises_violation("fk_accounts_ledger_id_ledgers"):
        await db_session.execute(delete(Ledger).where(Ledger.id == ledger.id))


async def test_account_with_ledger_entries_cannot_be_deleted(db_session: AsyncSession) -> None:
    payer = await create_customer_account(db_session)
    payee = await create_customer_account(db_session)
    transaction = await create_transaction(db_session, kind=TransactionKind.TRANSFER)
    add_entries(db_session, transaction, (payer, -3000), (payee, 3000))
    await check_deferred_constraints(db_session)

    async with raises_violation("fk_ledger_entries_account_id_ledger_id_accounts"):
        await db_session.execute(delete(Account).where(Account.id == payee.id))
