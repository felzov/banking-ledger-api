"""idempotency_keys: constraints and append-only protection (ADR 0006, migration 0010).

Every test runs on the rolled-back test connection. Deferred checks are forced with
check_deferred_constraints: the outer test transaction never commits.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import delete, insert, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.errors import sqlstate
from ledger_api.data.models import IdempotencyKey, Transaction, User
from ledger_api.domain.transaction import TransactionKind
from tests.database import check_deferred_constraints, raises_violation
from tests.factories import add_entries, create_customer_account, create_transaction, create_user

FINGERPRINT = bytes(32)


async def _posted_transaction_id(session: AsyncSession) -> uuid.UUID:
    payer = await create_customer_account(session, balance_minor=100)
    payee = await create_customer_account(session)
    transaction = await create_transaction(session)
    add_entries(session, transaction, (payer, -100), (payee, 100))
    # Deferred checks are not forced here: SET CONSTRAINTS ALL IMMEDIATE would stay in effect
    # for the rest of the test transaction. Tests that need them call it last.
    await session.flush()
    return transaction.id


async def _key_values(session: AsyncSession, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "key": "key-1",
        "request_fingerprint": FINGERPRINT,
    }
    if "user_id" not in overrides:
        values["user_id"] = (await create_user(session)).id
    if "transaction_id" not in overrides:
        values["transaction_id"] = await _posted_transaction_id(session)
    return {**values, **overrides}


async def _insert(session: AsyncSession, **overrides: Any) -> dict[str, Any]:
    """Raw INSERT: the database must enforce the rules by itself."""
    values = await _key_values(session, **overrides)
    await session.execute(insert(IdempotencyKey).values(**values))
    return values


# --- accepted keys -----------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["k", "!", "~", "K" * 255, "550e8400-e29b-41d4-a716-446655440000"])
async def test_visible_ascii_keys_of_1_to_255_characters_are_accepted(
    db_session: AsyncSession, key: str
) -> None:
    values = await _insert(db_session, key=key)
    await check_deferred_constraints(db_session)

    row = await db_session.get(IdempotencyKey, (values["user_id"], key))
    assert row is not None
    assert row.created_at is not None


async def test_the_same_key_may_be_used_by_different_users(db_session: AsyncSession) -> None:
    await _insert(db_session, key="shared")
    await _insert(db_session, key="shared")

    await check_deferred_constraints(db_session)


# --- CHECK constraints -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "",
        "K" * 256,
        "with space",
        "tab\tkey",
        "del\x7fkey",
        "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
        "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}",
        "line\nbreak",
    ],
)
async def test_keys_outside_visible_ascii_or_length_are_rejected(
    db_session: AsyncSession, key: str
) -> None:
    async with raises_violation("ck_idempotency_keys_key_format"):
        await _insert(db_session, key=key)


@pytest.mark.parametrize("fingerprint", [b"", bytes(31), bytes(33)])
async def test_fingerprint_must_be_32_bytes(db_session: AsyncSession, fingerprint: bytes) -> None:
    async with raises_violation("ck_idempotency_keys_fingerprint_length"):
        await _insert(db_session, request_fingerprint=fingerprint)


# --- keys --------------------------------------------------------------------------------------


async def test_a_key_is_unique_per_user(db_session: AsyncSession) -> None:
    first = await _insert(db_session)

    async with raises_violation("pk_idempotency_keys"):
        await _insert(db_session, user_id=first["user_id"])


async def test_a_transaction_has_at_most_one_key(db_session: AsyncSession) -> None:
    first = await _insert(db_session)

    async with raises_violation("uq_idempotency_keys_transaction_id"):
        await _insert(db_session, key="key-2", transaction_id=first["transaction_id"])


async def test_key_cannot_belong_to_an_unknown_user(db_session: AsyncSession) -> None:
    async with raises_violation("fk_idempotency_keys_user_id_users"):
        await _insert(db_session, user_id=uuid.uuid7())


async def test_transaction_reference_is_checked_at_commit_not_at_insert(
    db_session: AsyncSession,
) -> None:
    # The claim is inserted before the posting's header exists: the insert itself succeeds...
    await _insert(db_session, transaction_id=uuid.uuid7())

    # ...and the reference must resolve by COMMIT.
    async with raises_violation("fk_idempotency_keys_transaction_id_transactions"):
        await check_deferred_constraints(db_session)


async def test_header_inserted_after_the_key_satisfies_the_deferred_reference(
    db_session: AsyncSession,
) -> None:
    # The posting service's order: claim with a pre-generated id, then the header and entries.
    transaction_id = uuid.uuid7()
    await _insert(db_session, transaction_id=transaction_id)
    payer = await create_customer_account(db_session, balance_minor=100)
    payee = await create_customer_account(db_session)
    header = Transaction(
        id=transaction_id,
        ledger_id=payer.ledger_id,
        kind=TransactionKind.TRANSFER,
        entry_count=2,
    )
    db_session.add(header)
    await db_session.flush()
    add_entries(db_session, header, (payer, -100), (payee, 100))

    await check_deferred_constraints(db_session)


# --- append-only -------------------------------------------------------------------------------


async def test_key_cannot_be_updated(db_session: AsyncSession) -> None:
    values = await _insert(db_session)

    async with raises_violation("idempotency_keys_immutable"):
        await db_session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.user_id == values["user_id"])
            .values(request_fingerprint=b"\x01" * 32)
        )


async def test_key_cannot_be_deleted(db_session: AsyncSession) -> None:
    values = await _insert(db_session)

    async with raises_violation("idempotency_keys_immutable"):
        await db_session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.user_id == values["user_id"])
        )


async def test_idempotency_keys_cannot_be_truncated(db_session: AsyncSession) -> None:
    await _insert(db_session)
    # Settle the pending deferred foreign-key event first: otherwise PostgreSQL refuses the
    # TRUNCATE for that reason (55006) and the trigger under test never runs.
    await check_deferred_constraints(db_session)

    async with raises_violation("idempotency_keys_immutable"):
        await db_session.execute(text("TRUNCATE idempotency_keys"))


async def test_a_users_keys_keep_the_user_from_being_deleted(db_session: AsyncSession) -> None:
    values = await _insert(db_session)

    with pytest.raises(DBAPIError) as excinfo:
        await db_session.execute(delete(User).where(User.id == values["user_id"]))
    assert sqlstate(excinfo.value) == "23001"  # restrict_violation (ON DELETE RESTRICT)


async def test_trigger_function_pins_its_search_path(db_session: AsyncSession) -> None:
    config = await db_session.scalar(
        text(
            "SELECT proconfig FROM pg_proc WHERE oid = "
            "'public.forbid_idempotency_key_mutation()'::regprocedure"
        )
    )
    assert config == ["search_path=pg_catalog, pg_temp"]
