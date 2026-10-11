"""idempotency_keys: constraints, append-only protection (ADR 0006, migration 0010) and the
reciprocal link between a key and its transaction (migration 0011).

Every test runs on the rolled-back test connection. Deferred checks are forced with
check_deferred_constraints: the outer test transaction never commits. _insert writes a raw key
for rules checked at INSERT; anything that reaches the COMMIT-time checks is built as a real
keyed posting (_keyed_posting), in the service's order.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import delete, insert, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.errors import sqlstate
from ledger_api.data.models import Account, IdempotencyKey, Transaction, User
from ledger_api.domain.currency import Currency
from ledger_api.domain.transaction import TransactionKind
from tests.database import check_deferred_constraints, raises_violation
from tests.factories import (
    add_entries,
    create_customer_account,
    create_transaction,
    create_user,
    get_settlement_account,
)

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


async def _insert_key(
    session: AsyncSession, user_id: uuid.UUID, key: str, transaction_id: uuid.UUID
) -> None:
    await session.execute(
        insert(IdempotencyKey).values(
            user_id=user_id, key=key, request_fingerprint=FINGERPRINT, transaction_id=transaction_id
        )
    )


async def _header(
    session: AsyncSession,
    ledger_id: uuid.UUID,
    *,
    transaction_id: uuid.UUID | None = None,
    kind: TransactionKind = TransactionKind.TRANSFER,
    names: tuple[uuid.UUID, str] | None = None,
) -> Transaction:
    """A transaction header, optionally naming the key (user_id, key) it was posted under."""
    header = Transaction(ledger_id=ledger_id, kind=kind, entry_count=2)
    if transaction_id is not None:
        header.id = transaction_id
    if names is not None:
        header.idempotency_user_id, header.idempotency_key = names
    session.add(header)
    await session.flush()
    return header


async def _keyed_posting(
    session: AsyncSession, *, key: str = "key-1", user: User | None = None
) -> tuple[User, Account, Transaction]:
    """A keyed transfer exactly as the posting service writes it: the claim (with a
    pre-generated transaction id), then the header naming the key, then the entries. The key's
    user owns the debited account. Passes every COMMIT-time check."""
    owner = user or await create_user(session)
    payer = await create_customer_account(session, user=owner, balance_minor=100)
    payee = await create_customer_account(session)
    transaction_id = uuid.uuid7()
    await _insert_key(session, owner.id, key, transaction_id)
    header = await _header(
        session, payer.ledger_id, transaction_id=transaction_id, names=(owner.id, key)
    )
    add_entries(session, header, (payer, -100), (payee, 100))
    await session.flush()
    return owner, payer, header


# --- accepted keys -----------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["k", "!", "~", "K" * 255, "550e8400-e29b-41d4-a716-446655440000"])
async def test_visible_ascii_keys_of_1_to_255_characters_are_accepted(
    db_session: AsyncSession, key: str
) -> None:
    owner, _, _ = await _keyed_posting(db_session, key=key)
    await check_deferred_constraints(db_session)

    row = await db_session.get(IdempotencyKey, (owner.id, key))
    assert row is not None
    assert row.created_at is not None


async def test_the_same_key_may_be_used_by_different_users(db_session: AsyncSession) -> None:
    await _keyed_posting(db_session, key="shared")
    await _keyed_posting(db_session, key="shared")

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


async def test_header_inserted_after_the_key_satisfies_the_deferred_checks(
    db_session: AsyncSession,
) -> None:
    # The posting service's order: claim with a pre-generated id, then the header (naming the
    # key) and the entries. Every COMMIT-time check passes.
    owner, _, header = await _keyed_posting(db_session)

    await check_deferred_constraints(db_session)

    row = await db_session.get(IdempotencyKey, (owner.id, "key-1"))
    assert row is not None
    assert row.transaction_id == header.id
    assert (header.idempotency_user_id, header.idempotency_key) == (owner.id, "key-1")


# --- append-only -------------------------------------------------------------------------------


async def test_key_cannot_be_updated(db_session: AsyncSession) -> None:
    owner, _, _ = await _keyed_posting(db_session)

    async with raises_violation("idempotency_keys_immutable"):
        await db_session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.user_id == owner.id)
            .values(request_fingerprint=b"\x01" * 32)
        )


async def test_key_cannot_be_deleted(db_session: AsyncSession) -> None:
    owner, _, _ = await _keyed_posting(db_session)

    async with raises_violation("idempotency_keys_immutable"):
        await db_session.execute(delete(IdempotencyKey).where(IdempotencyKey.user_id == owner.id))


async def test_idempotency_keys_cannot_be_truncated(db_session: AsyncSession) -> None:
    await _keyed_posting(db_session)
    # Settle the pending deferred foreign-key event first: otherwise PostgreSQL refuses the
    # TRUNCATE for that reason (55006) and the trigger under test never runs.
    await check_deferred_constraints(db_session)

    # CASCADE: since 0011 transactions reference idempotency_keys, and that foreign key must not
    # be what stops the TRUNCATE.
    async with raises_violation("idempotency_keys_immutable"):
        await db_session.execute(text("TRUNCATE idempotency_keys CASCADE"))


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


# --- the reciprocal link between a key and its transaction (migration 0011) --------------------

LINK = "ck_idempotency_keys_transaction_link"
OWNER = "ck_idempotency_keys_transaction_owner"


async def test_a_key_cannot_be_attached_to_an_existing_unrelated_transaction(
    db_session: AsyncSession,
) -> None:
    # The forgery F2 describes: an older, unkeyed transaction, even one that debits the key's
    # user's own account, cannot be claimed by a new key. It names no key, and never can.
    owner = await create_user(db_session)
    payer = await create_customer_account(db_session, user=owner, balance_minor=100)
    payee = await create_customer_account(db_session)
    existing = await _header(db_session, payer.ledger_id)
    add_entries(db_session, existing, (payer, -100), (payee, 100))
    await db_session.flush()

    await _insert_key(db_session, owner.id, "forged", existing.id)

    async with raises_violation(LINK):
        await check_deferred_constraints(db_session)


async def test_a_key_cannot_be_attached_to_another_keys_transaction(
    db_session: AsyncSession,
) -> None:
    # Already keyed: the one-key-per-transaction rule (0010) refuses the second key outright.
    _, _, keyed = await _keyed_posting(db_session)
    intruder = await create_user(db_session)

    async with raises_violation("uq_idempotency_keys_transaction_id"):
        await _insert_key(db_session, intruder.id, "forged", keyed.id)


async def test_a_key_cannot_be_attached_to_another_users_transaction(
    db_session: AsyncSession,
) -> None:
    # Another user's unkeyed transfer: it names no key, so it cannot be claimed.
    victim_payer = await create_customer_account(db_session, balance_minor=100)
    payee = await create_customer_account(db_session)
    theirs = await _header(db_session, victim_payer.ledger_id)
    add_entries(db_session, theirs, (victim_payer, -100), (payee, 100))
    await db_session.flush()
    intruder = await create_user(db_session)

    await _insert_key(db_session, intruder.id, "forged", theirs.id)

    async with raises_violation(LINK):
        await check_deferred_constraints(db_session)


async def test_a_linked_pair_must_be_a_posting_of_the_keys_own_user(
    db_session: AsyncSession,
) -> None:
    # Key and header name each other, but the debited account belongs to someone else: the
    # pair claims to be the key owner's posting, and is not.
    key_owner = await create_user(db_session)
    victim_payer = await create_customer_account(db_session, balance_minor=100)
    payee = await create_customer_account(db_session)
    transaction_id = uuid.uuid7()
    await _insert_key(db_session, key_owner.id, "cross-user", transaction_id)
    header = await _header(
        db_session,
        victim_payer.ledger_id,
        transaction_id=transaction_id,
        names=(key_owner.id, "cross-user"),
    )
    add_entries(db_session, header, (victim_payer, -100), (payee, 100))
    await db_session.flush()

    async with raises_violation(OWNER):
        await check_deferred_constraints(db_session)


async def test_the_key_owner_must_be_the_debited_side_of_a_transfer(
    db_session: AsyncSession,
) -> None:
    # Credited by someone else's transfer is not "the key owner's posting".
    key_owner = await create_user(db_session)
    payer = await create_customer_account(db_session, balance_minor=100)
    owners_account = await create_customer_account(db_session, user=key_owner)
    transaction_id = uuid.uuid7()
    await _insert_key(db_session, key_owner.id, "payee", transaction_id)
    header = await _header(
        db_session, payer.ledger_id, transaction_id=transaction_id, names=(key_owner.id, "payee")
    )
    add_entries(db_session, header, (payer, -100), (owners_account, 100))
    await db_session.flush()

    async with raises_violation(OWNER):
        await check_deferred_constraints(db_session)


@pytest.mark.parametrize("kind", [TransactionKind.DEPOSIT, TransactionKind.WITHDRAWAL])
async def test_deposits_and_withdrawals_link_on_the_owners_side(
    db_session: AsyncSession, kind: TransactionKind
) -> None:
    owner = await create_user(db_session)
    account = await create_customer_account(db_session, user=owner, balance_minor=100)
    settlement = await get_settlement_account(db_session, Currency.GBP)
    customer_amount = 100 if kind == TransactionKind.DEPOSIT else -100
    transaction_id = uuid.uuid7()
    await _insert_key(db_session, owner.id, f"{kind}-key", transaction_id)
    header = await _header(
        db_session,
        account.ledger_id,
        transaction_id=transaction_id,
        kind=kind,
        names=(owner.id, f"{kind}-key"),
    )
    add_entries(db_session, header, (account, customer_amount), (settlement, -customer_amount))

    await check_deferred_constraints(db_session)  # credited by a deposit, debited by a withdrawal


LINK_TRIGGERS = "idempotency_keys_linked, transactions_idempotency_linked"


async def _mismatched_pair(session: AsyncSession) -> None:
    """A header naming key-b, while key-b references a DIFFERENT existing transaction.

    Every other rule is satisfied, so only the link check can object: both foreign keys
    resolve (key-b exists; its transaction exists), each key and each transaction is unique,
    the key's own transaction is a valid posting of its user, and every transaction balances.
    """
    owner, payer, _ = await _keyed_posting(session, key="key-a")
    payee = await create_customer_account(session)
    impostor = await _header(session, payer.ledger_id, names=(owner.id, "key-b"))
    add_entries(session, impostor, (payer, -10), (payee, 10))
    elsewhere = await _header(session, payer.ledger_id)  # unkeyed, the owner's own posting
    add_entries(session, elsewhere, (payer, -10), (payee, 10))
    await session.flush()
    await _insert_key(session, owner.id, "key-b", elsewhere.id)


async def test_a_key_must_reference_the_transaction_that_names_it(
    db_session: AsyncSession,
) -> None:
    await _mismatched_pair(db_session)

    # Only the link triggers run here, so the violation can only be theirs.
    async with raises_violation(LINK):
        await db_session.execute(text(f"SET CONSTRAINTS {LINK_TRIGGERS} IMMEDIATE"))


async def test_only_the_link_check_refuses_a_mismatched_pair(db_session: AsyncSession) -> None:
    # Control for the test above: with the link triggers removed (inside this rolled-back
    # transaction only; DDL is transactional), every other COMMIT-time check accepts the very
    # same rows. The link check is therefore the one and only thing refusing them.
    await db_session.execute(text("DROP TRIGGER idempotency_keys_linked ON idempotency_keys"))
    await db_session.execute(text("DROP TRIGGER transactions_idempotency_linked ON transactions"))
    await _mismatched_pair(db_session)

    await check_deferred_constraints(db_session)  # passes: nothing else objects


async def test_a_second_transaction_cannot_name_an_already_used_key(
    db_session: AsyncSession,
) -> None:
    # At most one transaction names a given key: refused at INSERT by the unique constraint.
    # (This does not exercise the reverse-link check; a new transaction naming a key that
    # points elsewhere is test_0011_keeps_0010_keys_valid_and_unhijackable.)
    owner, payer, _ = await _keyed_posting(db_session, key="taken")

    async with raises_violation("uq_transactions_idempotency_key"):
        await _header(db_session, payer.ledger_id, names=(owner.id, "taken"))


async def test_a_header_cannot_name_a_key_that_does_not_exist(db_session: AsyncSession) -> None:
    owner = await create_user(db_session)
    payer = await create_customer_account(db_session, user=owner, balance_minor=100)
    payee = await create_customer_account(db_session)
    header = await _header(db_session, payer.ledger_id, names=(owner.id, "missing"))
    add_entries(db_session, header, (payer, -100), (payee, 100))
    await db_session.flush()

    # The link trigger would refuse this too; checking only the foreign key isolates it.
    async with raises_violation("fk_transactions_idempotency_keys"):
        await db_session.execute(text("SET CONSTRAINTS fk_transactions_idempotency_keys IMMEDIATE"))


@pytest.mark.parametrize(
    "names", [{"idempotency_user_id": None, "idempotency_key": "k"}, {"idempotency_key": None}]
)
async def test_a_header_names_both_the_user_and_the_key_or_neither(
    db_session: AsyncSession, names: dict[str, Any]
) -> None:
    owner = await create_user(db_session)
    payer = await create_customer_account(db_session, user=owner)
    values = {"idempotency_user_id": owner.id, "idempotency_key": "k", **names}

    async with raises_violation("ck_transactions_idempotency_key_pair"):
        await db_session.execute(
            insert(Transaction).values(
                ledger_id=payer.ledger_id, kind="transfer", entry_count=2, **values
            )
        )


async def test_unkeyed_transactions_are_untouched_by_the_link_checks(
    db_session: AsyncSession,
) -> None:
    payer = await create_customer_account(db_session, balance_minor=100)
    payee = await create_customer_account(db_session)
    header = await _header(db_session, payer.ledger_id)
    add_entries(db_session, header, (payer, -100), (payee, 100))

    await check_deferred_constraints(db_session)
    assert (header.idempotency_user_id, header.idempotency_key) == (None, None)


async def test_temporary_tables_cannot_fake_the_link(db_session: AsyncSession) -> None:
    # As in 0008/0009: the check names public.* tables under a pinned search_path, so a
    # session's own temporary "idempotency_keys" and "transactions" claiming a valid link are
    # never read.
    owner = await create_user(db_session)
    payer = await create_customer_account(db_session, user=owner, balance_minor=100)
    payee = await create_customer_account(db_session)
    existing = await _header(db_session, payer.ledger_id)
    add_entries(db_session, existing, (payer, -100), (payee, 100))
    await db_session.flush()
    # The forged key, in the real table: written BEFORE the shadows exist, since an unqualified
    # INSERT would otherwise land in the temporary table.
    await _insert_key(db_session, owner.id, "forged", existing.id)
    await db_session.execute(
        text("CREATE TEMP TABLE idempotency_keys (user_id uuid, key text, transaction_id uuid)")
    )
    await db_session.execute(
        text(
            "CREATE TEMP TABLE transactions (id uuid, kind text, idempotency_user_id uuid, "
            "idempotency_key text)"
        )
    )
    await db_session.execute(
        text("INSERT INTO pg_temp.idempotency_keys VALUES (:u, 'forged', :t)"),
        {"u": owner.id, "t": existing.id},
    )
    await db_session.execute(
        text("INSERT INTO pg_temp.transactions VALUES (:t, 'transfer', :u, 'forged')"),
        {"u": owner.id, "t": existing.id},
    )

    async with raises_violation(LINK):
        await check_deferred_constraints(db_session)


async def test_link_function_pins_its_search_path_and_runs_as_the_invoker(
    db_session: AsyncSession,
) -> None:
    config, definer = (
        await db_session.execute(
            text(
                "SELECT proconfig, prosecdef FROM pg_proc WHERE oid = "
                "'public.assert_idempotency_link()'::regprocedure"
            )
        )
    ).one()
    assert config == ["search_path=pg_catalog, pg_temp"]
    assert definer is False
