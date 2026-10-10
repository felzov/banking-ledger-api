"""Postings under a least-privilege role (migration 0007, ADR 0012).

posting_role_grants() (tests/database.py) is the privilege contract of a role that can run
the posting services: table privileges plus EXECUTE on the sequence helper, and NOTHING on the
entries' identity sequence (inserting into an identity column does not need it). Before 0007
the ordering trigger read the sequence with the caller's privileges, got NULL, and rejected
every entry.

Roles are cluster-wide. The rolled-back tests create theirs inside the test transaction; the
concurrent tests commit one and drop it afterwards.

Every test checks, from inside the transactions under test, that they ran as the role and not
as the superuser (role_witness): a test that silently fell back to the superuser would pass
whatever the privileges, because a superuser bypasses them all.
"""

import asyncio
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import URL, Connection, event, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ledger_api.data import posting as data_posting
from ledger_api.data.engine import create_sessionmaker
from ledger_api.data.errors import sqlstate, violated_constraint
from ledger_api.data.models import Account, AuditEvent, LedgerEntry
from ledger_api.domain.audit import AuditOutcome
from ledger_api.domain.errors import InsufficientFundsError
from ledger_api.services import audit as audit_service
from ledger_api.services import posting as posting_service
from ledger_api.services.posting import PostingResult, deposit, transfer, withdraw
from tests.conftest import SessionFactory
from tests.database import (
    SEQUENCE_HELPER,
    assert_reconciled,
    audit_events_about,
    posting_role_grants,
    raises_violation,
)
from tests.factories import commit_funded_account, create_customer_account

HELPER = SEQUENCE_HELPER
SEQUENCE = "public.ledger_entries_sequence_number_seq"


def _role_name() -> str:
    return f"ledger_posting_{uuid.uuid7().hex}"


async def _create_role(connection: AsyncConnection, role: str, *, helper: bool = True) -> None:
    await connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
    for grant in posting_role_grants(role, helper=helper):
        await connection.execute(text(grant))


# --- who actually ran it --------------------------------------------------------------------------


@dataclass
class RoleWitness:
    """current_user and is_superuser, read inside every posting attempt (when it locks its
    accounts) and every audit write, in the same transaction as the work."""

    seen: list[tuple[str, str, bool]] = field(default_factory=list)

    def forget_setup(self) -> None:
        """Drop what test setup recorded (it posts as the superuser, by design)."""
        self.seen.clear()

    def assert_ran_as(self, role: str, *, attempts: int, audits: int) -> None:
        steps = Counter(step for step, _, _ in self.seen)
        assert steps == {"attempt": attempts, "audit": audits}, steps
        assert {(user, superuser) for _, user, superuser in self.seen} == {(role, False)}


@pytest.fixture
def role_witness(monkeypatch: pytest.MonkeyPatch) -> RoleWitness:
    witness = RoleWitness()

    def watch(module: Any, name: str, step: str) -> None:
        real = getattr(module, name)

        async def watched(session: AsyncSession, *args: Any, **kwargs: Any) -> Any:
            user, superuser = (
                await session.execute(
                    text("SELECT current_user, current_setting('is_superuser') = 'on'")
                )
            ).one()
            witness.seen.append((step, user, superuser))
            return await real(session, *args, **kwargs)

        monkeypatch.setattr(module, name, watched)

    watch(posting_service, "lock_accounts", "attempt")  # first step of every posting attempt
    watch(audit_service, "insert_audit_event", "audit")  # success, rejected and failed events
    return witness


async def _current_role(session: AsyncSession) -> tuple[str, bool]:
    user, superuser = (
        await session.execute(text("SELECT current_user, current_setting('is_superuser') = 'on'"))
    ).one()
    return user, superuser


# --- rolled back: one connection, the role switched on it ---------------------------------------


@pytest.fixture
async def pair(db_session: AsyncSession) -> tuple[Account, Account]:
    """Two GBP accounts without entries, created as the superuser test role. No entry is
    inserted, so the ordering trigger has not run in this transaction yet."""
    return await create_customer_account(db_session), await create_customer_account(db_session)


async def _fund(new_session: SessionFactory, account: Account, amount: int) -> PostingResult:
    assert account.user_id is not None
    return await deposit(
        new_session(), owner_id=account.user_id, account_id=account.id, amount_minor=amount
    )


async def _switch_to_new_posting_role(
    db_connection: AsyncConnection, *, helper: bool = True
) -> str:
    """Create a posting role inside the test transaction and become it (SET LOCAL ROLE).

    Call it BEFORE the test's first ledger entry. PL/pgSQL keeps the evaluation state of the
    trigger's simple expressions, including the EXECUTE check on the helper, for the rest of
    the transaction: had the superuser fired the trigger first, the role would inherit that
    check and a missing grant would go unnoticed. (Production connections do not switch roles
    inside a transaction; the committed tests below run as the role only.)
    """
    role = _role_name()
    await _create_role(db_connection, role, helper=helper)
    await db_connection.execute(text(f'SET LOCAL ROLE "{role}"'))
    assert await db_connection.scalar(text("SELECT current_user")) == role
    return role


async def test_posting_role_cannot_read_the_sequence_itself(
    db_connection: AsyncConnection, new_session: SessionFactory, pair: tuple[Account, Account]
) -> None:
    # The cause of the 0006 bug, shown directly: the role has no privilege on the sequence,
    # so pg_sequence_last_value() returns NULL and a direct read is refused.
    await _switch_to_new_posting_role(db_connection)
    await _fund(new_session, pair[0], 10)  # the sequence has issued a value, even on a new DB

    can_read = await db_connection.scalar(
        text(f"SELECT has_sequence_privilege('{SEQUENCE}', 'SELECT,USAGE')")
    )
    via_builtin = await db_connection.scalar(
        text(f"SELECT pg_sequence_last_value('{SEQUENCE}'::regclass)")
    )
    via_helper = await db_connection.scalar(text(f"SELECT {HELPER}"))

    assert can_read is False
    assert via_builtin is None
    assert isinstance(via_helper, int)  # the one value the role may know, through the helper
    nested = await db_connection.begin_nested()
    with pytest.raises(DBAPIError) as excinfo:
        await db_connection.execute(
            text("SELECT last_value FROM public.ledger_entries_sequence_number_seq")
        )
    await nested.rollback()
    assert sqlstate(excinfo.value) == "42501"  # insufficient_privilege


async def test_posting_role_deposits_withdraws_and_transfers(
    db_connection: AsyncConnection,
    db_session: AsyncSession,
    new_session: SessionFactory,
    pair: tuple[Account, Account],
    role_witness: RoleWitness,
) -> None:
    payer, payee = pair
    assert payer.user_id is not None
    role = await _switch_to_new_posting_role(db_connection)
    await _fund(new_session, payer, 1_000)

    deposited = await deposit(
        new_session(), owner_id=payer.user_id, account_id=payer.id, amount_minor=500
    )
    withdrawn = await withdraw(
        new_session(), owner_id=payer.user_id, account_id=payer.id, amount_minor=200
    )
    moved = await transfer(
        new_session(),
        owner_id=payer.user_id,
        source_account_id=payer.id,
        destination_account_id=payee.id,
        amount_minor=300,
    )
    with pytest.raises(InsufficientFundsError):  # rejections (and their audit) work too
        await withdraw(
            new_session(), owner_id=payer.user_id, account_id=payer.id, amount_minor=10_000
        )

    role_witness.assert_ran_as(role, attempts=5, audits=5)
    assert deposited.balances == {payer.id: 1_500}
    assert withdrawn.balances == {payer.id: 1_300}
    assert moved.balances == {payer.id: 1_000}
    await assert_reconciled(db_session)
    events = await audit_events_about(db_session, payer.id)
    assert [event.outcome for event in events] == [
        AuditOutcome.SUCCEEDED,  # the funding deposit
        AuditOutcome.SUCCEEDED,
        AuditOutcome.SUCCEEDED,
        AuditOutcome.SUCCEEDED,
        AuditOutcome.REJECTED,
    ]


_INSERT_HEADER = text(
    "INSERT INTO transactions (ledger_id, kind, entry_count) "
    "VALUES (:ledger, 'transfer', 2) RETURNING id"
)
_INSERT_NUMBERED = text(
    "INSERT INTO ledger_entries "
    "(transaction_id, account_id, ledger_id, amount_minor, sequence_number) "
    "OVERRIDING SYSTEM VALUE VALUES "
    "(:t, :payer, :l, -1, :payer_number), (:t, :payee, :l, 1, :payee_number)"
)


@pytest.mark.parametrize(("offset", "constraint"), [(-1_000_000, "monotonic"), (1_000, "issued")])
async def test_backdated_and_unissued_numbers_stay_rejected_for_the_role(
    db_connection: AsyncConnection,
    db_session: AsyncSession,
    new_session: SessionFactory,
    pair: tuple[Account, Account],
    offset: int,
    constraint: str,
) -> None:
    payer, payee = pair
    await _switch_to_new_posting_role(db_connection)
    await _fund(new_session, payer, 100)  # an existing history, posted as the role
    issued = await db_connection.scalar(text(f"SELECT {HELPER}"))
    assert isinstance(issued, int)
    number = max(issued + offset, 1)

    async with raises_violation(f"ck_ledger_entries_sequence_{constraint}"):
        transaction_id = await db_session.scalar(_INSERT_HEADER, {"ledger": payer.ledger_id})
        await db_session.execute(
            _INSERT_NUMBERED,
            {
                "t": transaction_id,
                "payer": payer.id,
                "payee": payee.id,
                "l": payer.ledger_id,
                "payer_number": number,
                "payee_number": number,
            },
        )


async def _shadow_protected_tables(db_connection: AsyncConnection) -> None:
    """Temporary tables named like the ones the ordering trigger reads. Temporary tables need
    no grant (PUBLIC has TEMP) and, for unqualified names, the temporary schema is searched
    first, so before migration 0008 the trigger locked and read these instead."""
    await db_connection.execute(text("CREATE TEMP TABLE accounts (LIKE public.accounts)"))
    await db_connection.execute(
        text("CREATE TEMP TABLE ledger_entries (LIKE public.ledger_entries)")
    )
    await db_connection.execute(text("CREATE TEMP TABLE transactions (LIKE public.transactions)"))


async def _mirror_into_shadows(db_connection: AsyncConnection) -> None:
    """Copy the real rows into the shadows, so that commit-time checks reading the shadows
    (the deferred checks of migrations 0001/0002 are not pinned) find every pending
    transaction valid: what is left to reject a bad entry is the ordering trigger alone."""
    for table in ("accounts", "ledger_entries", "transactions"):
        await db_connection.execute(
            text(f"INSERT INTO pg_temp.{table} SELECT * FROM public.{table}")  # noqa: S608
        )


_INSERT_HEADER_QUALIFIED = text(
    "INSERT INTO public.transactions (ledger_id, kind, entry_count) "
    "VALUES (:ledger, 'transfer', 2) RETURNING id"
)


async def test_temporary_tables_cannot_hide_an_accounts_history(
    db_connection: AsyncConnection,
    db_session: AsyncSession,
    new_session: SessionFactory,
    pair: tuple[Account, Account],
) -> None:
    # Before 0008 the trigger saw the empty shadow tables, found no history for the payer and
    # accepted the backdated entry; the commit-time checks passed too (on the mirrored rows).
    payer, payee = pair
    newcomer = await create_customer_account(db_session)  # no entries: any number is "after"
    await _switch_to_new_posting_role(db_connection)
    await _fund(new_session, payee, 100)  # numbers below the payer's...
    await _fund(new_session, payer, 100)  # ...latest one, unused on the payer's account
    backdated = await db_connection.scalar(
        text("SELECT min(sequence_number) FROM public.ledger_entries WHERE account_id = :a"),
        {"a": payee.id},
    )
    latest = await db_connection.scalar(
        text("SELECT max(sequence_number) FROM public.ledger_entries WHERE account_id = :a"),
        {"a": payer.id},
    )
    assert backdated < latest
    await _shadow_protected_tables(db_connection)

    async with raises_violation("ck_ledger_entries_sequence_monotonic"):
        transaction_id = await db_session.scalar(
            _INSERT_HEADER_QUALIFIED, {"ledger": payer.ledger_id}
        )
        await db_session.execute(
            text(
                "INSERT INTO public.ledger_entries "
                "(transaction_id, account_id, ledger_id, amount_minor, sequence_number) "
                "OVERRIDING SYSTEM VALUE VALUES "
                "(:t, :payer, :l, -1, :number), (:t, :newcomer, :l, 1, :number)"
            ),
            {
                "t": transaction_id,
                "payer": payer.id,
                "newcomer": newcomer.id,
                "l": payer.ledger_id,
                "number": backdated,
            },
        )
        # Reached only if the trigger accepted the entry: force the commit-time checks, so
        # that the test fails (nothing raised) rather than passing on a deferred error.
        await _mirror_into_shadows(db_connection)
        await db_connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


async def test_temporary_tables_do_not_block_a_valid_entry(
    db_connection: AsyncConnection,
    db_session: AsyncSession,
    new_session: SessionFactory,
    pair: tuple[Account, Account],
) -> None:
    payer, payee = pair
    await _switch_to_new_posting_role(db_connection)
    await _fund(new_session, payer, 100)
    await _shadow_protected_tables(db_connection)

    transaction_id = await db_session.scalar(_INSERT_HEADER_QUALIFIED, {"ledger": payer.ledger_id})
    await db_session.execute(
        text(
            "INSERT INTO public.ledger_entries "
            "(transaction_id, account_id, ledger_id, amount_minor) "
            "VALUES (:t, :payer, :l, -1), (:t, :payee, :l, 1)"
        ),
        {"t": transaction_id, "payer": payer.id, "payee": payee.id, "l": payer.ledger_id},
    )
    await _mirror_into_shadows(db_connection)
    await db_connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    numbers = await db_connection.scalars(
        text(
            "SELECT sequence_number FROM public.ledger_entries "
            "WHERE account_id = :a ORDER BY sequence_number"
        ),
        {"a": payer.id},
    )
    funding, new = numbers.all()
    assert new > funding  # numbered by the identity, after the account's history


async def test_role_without_execute_on_the_helper_fails_loudly(
    db_connection: AsyncConnection,
    new_session: SessionFactory,
    pair: tuple[Account, Account],
) -> None:
    # A forgotten grant is a clear permission error naming the function, not a misleading
    # "never issued" check violation.
    payer, _ = pair
    assert payer.user_id is not None
    await _switch_to_new_posting_role(db_connection, helper=False)

    with pytest.raises(DBAPIError) as excinfo:
        await deposit(new_session(), owner_id=payer.user_id, account_id=payer.id, amount_minor=1)

    assert sqlstate(excinfo.value) == "42501"
    assert "ledger_entries_last_issued_sequence_number" in str(excinfo.value.orig)


# --- the helper's boundary -----------------------------------------------------------------------


async def test_helper_is_a_narrow_security_definer(db_session: AsyncSession) -> None:
    row = (
        await db_session.execute(
            text(
                "SELECT p.pronargs, p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner), "
                "       t.tableowner, "
                "       EXISTS (SELECT 1 FROM aclexplode(p.proacl) a "
                "               WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE') "
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "  JOIN pg_tables t ON t.schemaname = 'public' "
                "                  AND t.tablename = 'ledger_entries' "
                " WHERE n.nspname = 'public' "
                "   AND p.proname = 'ledger_entries_last_issued_sequence_number'"
            )
        )
    ).one()
    arguments, definer, config, owner, table_owner, public_can_execute = row

    assert arguments == 0  # nothing to point it at another sequence
    assert definer is True
    assert config == ["search_path=pg_catalog, pg_temp"]
    assert owner == table_owner  # the definer owns the sequence it reads
    assert public_can_execute is False
    # The trigger function itself stays SECURITY INVOKER (locks and reads run as the caller),
    # with its search_path pinned so temporary tables cannot shadow its tables (0008).
    trigger_definer, trigger_config = (
        await db_session.execute(
            text(
                "SELECT prosecdef, proconfig FROM pg_proc "
                "WHERE oid = 'public.assert_entry_sequence_order'::regproc"
            )
        )
    ).one()
    assert trigger_definer is False
    assert trigger_config == ["search_path=pg_catalog, pg_temp"]


async def test_helper_cannot_be_used_to_read_other_sequences(
    db_connection: AsyncConnection, pair: tuple[Account, Account]
) -> None:
    await _switch_to_new_posting_role(db_connection)

    # It takes no argument: there is no way to name another sequence...
    nested = await db_connection.begin_nested()
    with pytest.raises(DBAPIError) as excinfo:
        await db_connection.execute(
            text(
                "SELECT public.ledger_entries_last_issued_sequence_number("
                "'public.audit_events_id_seq'::regclass)"
            )
        )
    await nested.rollback()
    assert sqlstate(excinfo.value) == "42883"  # undefined_function
    # ...and the role still cannot read any other sequence itself.
    other = await db_connection.scalar(
        text("SELECT pg_sequence_last_value('public.audit_events_id_seq'::regclass)")
    )
    assert other is None


# --- concurrent: committed postings as the role --------------------------------------------------


@dataclass(frozen=True)
class RoleSessions:
    role: str
    sessionmaker: async_sessionmaker[AsyncSession]


@pytest.fixture
async def posting_role(engine: AsyncEngine, test_database_url: URL) -> AsyncIterator[RoleSessions]:
    """Sessions whose every transaction runs as a committed least-privilege role.

    The role is set per transaction (SET LOCAL ROLE when it begins), never once per pooled
    connection: the asyncpg adapter runs a connect-time SET inside a transaction that the
    pool's rollback-on-return undoes, so a reused connection was back to the superuser.
    """
    role = _role_name()
    role_engine = create_async_engine(test_database_url)

    @event.listens_for(role_engine.sync_engine, "begin")
    def become_role(connection: Connection) -> None:
        # Lasts exactly this transaction: neither pooling nor a rollback can carry another
        # role into, or this role out of, it.
        connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')

    try:
        async with engine.begin() as connection:
            await _create_role(connection, role)
        yield RoleSessions(role, create_sessionmaker(role_engine))
    finally:
        try:
            await role_engine.dispose()
        finally:
            async with engine.begin() as connection:
                if await connection.scalar(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
                ):
                    await connection.execute(text(f'DROP OWNED BY "{role}"'))  # its grants
                    await connection.execute(text(f'DROP ROLE "{role}"'))


@pytest.fixture
def no_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(posting_service, "MAX_ATTEMPTS", 1)


type Operation = Callable[[AsyncSession], Awaitable[PostingResult]]


async def _race(
    sessionmaker: async_sessionmaker[AsyncSession], operations: list[Operation]
) -> list[PostingResult | BaseException]:
    start = asyncio.Event()

    async def run(operation: Operation) -> PostingResult:
        async with sessionmaker() as session:
            await start.wait()
            return await operation(session)

    tasks = [asyncio.create_task(run(operation)) for operation in operations]
    await asyncio.sleep(0)
    start.set()
    return list(await asyncio.gather(*tasks, return_exceptions=True))


@pytest.mark.concurrency
@pytest.mark.usefixtures("no_retries")
async def test_concurrent_withdrawals_as_the_role(
    engine: AsyncEngine, posting_role: RoleSessions, role_witness: RoleWitness
) -> None:
    superuser = create_sessionmaker(engine)
    account = await commit_funded_account(superuser, 100)  # setup needs INSERT on users
    role_witness.forget_setup()

    async def withdraw_30(session: AsyncSession) -> PostingResult:
        return await withdraw(
            session, owner_id=account.owner_id, account_id=account.account_id, amount_minor=30
        )

    results = await _race(posting_role.sessionmaker, [withdraw_30] * 10)

    # Ten attempts; three success events and seven rejected ones, each written as the role.
    role_witness.assert_ran_as(posting_role.role, attempts=10, audits=10)
    assert Counter(type(r).__name__ for r in results) == {
        "PostingResult": 3,
        "InsufficientFundsError": 7,
    }
    async with superuser() as session:
        balance = await session.scalar(
            select(Account.balance_minor).where(Account.id == account.account_id)
        )
        assert balance == 10
        await assert_reconciled(session)
        _funding, *events = await audit_events_about(session, account.account_id)
    assert Counter(event.outcome for event in events) == {
        AuditOutcome.SUCCEEDED: 3,
        AuditOutcome.REJECTED: 7,
    }


@pytest.mark.concurrency
@pytest.mark.usefixtures("no_retries")
async def test_opposite_transfers_as_the_role_do_not_deadlock(
    engine: AsyncEngine, posting_role: RoleSessions, role_witness: RoleWitness
) -> None:
    # The trigger's lock is a re-acquisition inside the protocol: no new lock order.
    superuser = create_sessionmaker(engine)
    first = await commit_funded_account(superuser, 10_000)
    second = await commit_funded_account(superuser, 10_000)
    role_witness.forget_setup()

    def send(source: Any, destination: Any) -> Operation:
        async def operation(session: AsyncSession) -> PostingResult:
            return await transfer(
                session,
                owner_id=source.owner_id,
                source_account_id=source.account_id,
                destination_account_id=destination.account_id,
                amount_minor=100,
            )

        return operation

    results = await _race(
        posting_role.sessionmaker, [send(first, second), send(second, first)] * 20
    )

    assert Counter(type(r).__name__ for r in results) == {"PostingResult": 40}
    # Forty sessions, more than the pool holds: connections are reused, still as the role.
    role_witness.assert_ran_as(posting_role.role, attempts=40, audits=40)
    async with superuser() as session:
        await assert_reconciled(session)
        transfers = await session.scalar(
            select(func.count()).where(
                AuditEvent.outcome == AuditOutcome.SUCCEEDED,
                AuditEvent.actor_user_id.in_([first.owner_id, second.owner_id]),
                AuditEvent.transaction_id.is_not(None),
            )
        )
    assert transfers == 2 + 40  # two funding deposits, forty transfers


@pytest.mark.concurrency
@pytest.mark.usefixtures("no_retries")
async def test_role_cannot_backdate_behind_an_uncommitted_posting(
    engine: AsyncEngine,
    posting_role: RoleSessions,
    role_witness: RoleWitness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The F1 race, with the raw writer running as the least-privilege role: it waits on the
    # trigger's account lock, then sees the committed entry and is rejected.
    superuser = create_sessionmaker(engine)
    account = await commit_funded_account(superuser, 1_000)
    await commit_funded_account(superuser, 1_000)  # draws numbers: room after A's latest
    async with superuser() as session:
        latest = await session.scalar(
            select(func.max(LedgerEntry.sequence_number)).where(
                LedgerEntry.account_id == account.account_id
            )
        )
        ledger_id = await session.scalar(
            select(Account.ledger_id).where(Account.id == account.account_id)
        )
        counterparty = await create_customer_account(session)
        await session.commit()
    assert latest is not None
    role_witness.forget_setup()

    wrote, go = asyncio.Event(), asyncio.Event()
    real_apply = data_posting.apply_balance_deltas

    async def pause_after_entries(*args: Any, **kwargs: Any) -> Any:
        result = await real_apply(*args, **kwargs)
        task = asyncio.current_task()
        if task is not None and task.get_name() == "posting":
            wrote.set()
            await go.wait()
        return result

    monkeypatch.setattr(posting_service, "apply_balance_deltas", pause_after_entries)

    async def run_posting() -> PostingResult:
        async with posting_role.sessionmaker() as session:
            return await deposit(
                session, owner_id=account.owner_id, account_id=account.account_id, amount_minor=7
            )

    async def backdate() -> None:
        async with posting_role.sessionmaker() as session:
            # Same transaction as the insert below: the raw writer is the role, not the superuser.
            assert await _current_role(session) == (posting_role.role, False)
            transaction_id = await session.scalar(_INSERT_HEADER, {"ledger": ledger_id})
            await session.execute(
                _INSERT_NUMBERED,
                {
                    "t": transaction_id,
                    "payer": counterparty.id,
                    "payee": account.account_id,
                    "l": ledger_id,
                    "payer_number": latest + 1,
                    "payee_number": latest + 1,
                },
            )

    posting = asyncio.create_task(run_posting(), name="posting")
    await wrote.wait()
    raw = asyncio.create_task(backdate())
    async with engine.connect() as probe, asyncio.timeout(5):
        while not await probe.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        ):
            await probe.rollback()
            await asyncio.sleep(0.02)
    go.set()

    await posting
    with pytest.raises(IntegrityError) as excinfo:
        await raw
    assert violated_constraint(excinfo.value) == "ck_ledger_entries_sequence_monotonic"
    role_witness.assert_ran_as(posting_role.role, attempts=1, audits=1)  # the posting


# --- the role checks themselves ----------------------------------------------------------------


async def test_every_transaction_of_a_role_session_runs_as_the_role(
    posting_role: RoleSessions,
) -> None:
    # The old fixture's failure: SET ROLE once per pooled connection was undone by the pool's
    # rollback-on-return, so every session after the first on a connection ran as the
    # superuser. Sequential sessions reuse the same pooled connection; so does a second
    # transaction after a rollback.
    for _ in range(3):
        async with posting_role.sessionmaker() as session:
            assert await _current_role(session) == (posting_role.role, False)
            await session.rollback()
            assert await _current_role(session) == (posting_role.role, False)
            await session.commit()


async def test_the_witness_catches_an_operation_run_as_the_superuser(
    engine: AsyncEngine, posting_role: RoleSessions, role_witness: RoleWitness
) -> None:
    superuser = create_sessionmaker(engine)
    account = await commit_funded_account(superuser, 100)
    role_witness.forget_setup()

    async with superuser() as session:  # the wrong sessions
        await withdraw(
            session, owner_id=account.owner_id, account_id=account.account_id, amount_minor=1
        )

    with pytest.raises(AssertionError):
        role_witness.assert_ran_as(posting_role.role, attempts=1, audits=1)
