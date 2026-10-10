"""Money movement: deposits, withdrawals and transfers (ADR 0010).

Lifecycle of every posting, inside ONE database transaction:

  1. pre-validate (pure, before BEGIN): amount, distinct accounts
  2. BEGIN
     claim the idempotency key, if any (ADR 0006): the first write, before any lock. A key
     already consumed by a committed posting either replays it (same fingerprint: return the
     original result, write nothing) or is refused (idempotency_key_mismatch)
  3. resolve immutable facts (the settlement account of an account's ledger)
  4. lock every account of the posting: one statement, ascending id, FOR UPDATE OF accounts
  5. validate against the locked rows only: ownership, counterparty, currency, funds
  6. write: transaction header (sealed with entry_count), entries, relative balance updates,
     and, last, the succeeded audit event (ADR 0011)
  7. COMMIT: deferred triggers check >= 2 entries, sum zero, count = entry_count

Any error rolls back everything, the success audit event included, releasing the locks:
nothing partial is ever committed. No I/O other than this session happens between BEGIN and
COMMIT. A rejected or failed posting is audited after the rollback, in a transaction of its
own (services.audit.audited), and its original error is re-raised unchanged.

Transient failures retry the WHOLE transaction (steps 2-7), never a part of it: a deadlock
or serialization failure up to MAX_ATTEMPTS in total, with jittered backoff. A lock or
statement timeout is not retried (the caller already waited); it, and exhausted retries,
become TemporarilyUnavailableError. Retrying is safe because a failed attempt committed
nothing, its key claim included: rejected and failed postings never leave a key behind.

A failure DURING COMMIT is different: unless PostgreSQL itself answered the COMMIT with a
non-fatal ERROR on a connection that is still open, the posting may have been applied, e.g.
when the connection dropped before the acknowledgement arrived. That is
CommitOutcomeUnknownError (outcome_unknown): never retried here, never reported as a failure.
The client retries with the same idempotency key; the claim then waits for, or finds, the
durable record.
"""

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.errors import server_severity, sqlstate, violated_constraint
from ledger_api.data.idempotency import claim_idempotency_key, find_posted_entry
from ledger_api.data.posting import (
    LockedAccount,
    apply_balance_deltas,
    find_settlement_account_id,
    insert_posting,
    lock_accounts,
)
from ledger_api.domain.account import AccountKind
from ledger_api.domain.audit import AuditAction, DetailValue
from ledger_api.domain.errors import (
    AccountNotFoundError,
    CommitOutcomeUnknownError,
    CurrencyMismatchError,
    DestinationAccountNotFoundError,
    IdempotencyKeyMismatchError,
    InsufficientFundsError,
    TemporarilyUnavailableError,
)
from ledger_api.domain.idempotency import is_valid_idempotency_key, request_fingerprint
from ledger_api.domain.posting import (
    Posting,
    deposit_posting,
    transfer_posting,
    validate_amount,
    withdrawal_posting,
)
from ledger_api.domain.transaction import TransactionKind
from ledger_api.services.audit import AuditContext, audited, record_in_transaction

logger = logging.getLogger(__name__)

# Attempts per posting, the first one included (ADR 0005). Concurrency tests lower it to 1 so
# that a deadlock cannot be hidden by a retry.
MAX_ATTEMPTS = 3
# deadlock_detected, serialization_failure: the transaction was rolled back; retry it whole.
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
# lock_not_available (lock_timeout), query_canceled (statement_timeout): already waited.
CONTENTION_SQLSTATES = frozenset({"55P03", "57014"})
RETRY_BACKOFF_SECONDS = (0.01, 0.05)
# SQLSTATEs that leave a COMMIT's outcome unknown even when PostgreSQL reports them as a
# non-fatal ERROR on a live connection: statement_completion_unknown and
# transaction_resolution_unknown. Class 08 (connection_exception) is treated the same way.
UNKNOWN_OUTCOME_SQLSTATES = frozenset({"40003", "08007"})
CONNECTION_EXCEPTION_CLASS = "08"
# An idempotency key names a user that does not exist: no account of theirs can either.
KEY_OWNER_MUST_EXIST = "fk_idempotency_keys_user_id_users"


@dataclass(frozen=True, slots=True)
class PostingResult:
    transaction_id: uuid.UUID
    kind: TransactionKind
    created_at: datetime
    # New balances of the caller's own accounts only: never a counterparty's (ADR 0010). On a
    # replay, the balances right after the ORIGINAL posting, not the current ones.
    balances: Mapping[uuid.UUID, int]
    # True if an earlier request with the same idempotency key had already posted this.
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class _KeyedRequest:
    """A posting request's idempotency key, and the fingerprint of what it asks for."""

    owner_id: uuid.UUID
    # The caller's account: the one whose balance the result reports.
    account_id: uuid.UUID
    key: str
    fingerprint: bytes


async def deposit(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    amount_minor: int,
    idempotency_key: str | None = None,
) -> PostingResult:
    """Credit owner_id's account with money arriving from outside (the settlement account)."""
    _check_key_format(idempotency_key)
    context = AuditContext(
        AuditAction.POSTING_DEPOSIT,
        owner_id,
        {"account_id": account_id, "amount_minor": _amount_detail(amount_minor)},
        idempotency_key=idempotency_key,
    )

    async def run() -> PostingResult:
        validate_amount(amount_minor)
        keyed = _keyed_request(
            idempotency_key,
            operation=TransactionKind.DEPOSIT,
            owner_id=owner_id,
            account_id=account_id,
            amount_minor=amount_minor,
        )

        async def attempt(transaction_id: uuid.UUID) -> PostingResult:
            settlement_id = await _settlement_account_of(session, account_id)
            posting = deposit_posting(
                account_id=account_id,
                settlement_account_id=settlement_id,
                amount_minor=amount_minor,
            )
            return await _post(
                session,
                posting,
                context,
                transaction_id,
                owner_id=owner_id,
                owned={account_id},
                link=_link(keyed),
            )

        return await _in_transaction(session, context, attempt, keyed)

    return await audited(session, context, run)


async def withdraw(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    amount_minor: int,
    idempotency_key: str | None = None,
) -> PostingResult:
    """Debit owner_id's account with money leaving to outside (the settlement account)."""
    _check_key_format(idempotency_key)
    context = AuditContext(
        AuditAction.POSTING_WITHDRAWAL,
        owner_id,
        {"account_id": account_id, "amount_minor": _amount_detail(amount_minor)},
        idempotency_key=idempotency_key,
    )

    async def run() -> PostingResult:
        validate_amount(amount_minor)
        keyed = _keyed_request(
            idempotency_key,
            operation=TransactionKind.WITHDRAWAL,
            owner_id=owner_id,
            account_id=account_id,
            amount_minor=amount_minor,
        )

        async def attempt(transaction_id: uuid.UUID) -> PostingResult:
            settlement_id = await _settlement_account_of(session, account_id)
            posting = withdrawal_posting(
                account_id=account_id,
                settlement_account_id=settlement_id,
                amount_minor=amount_minor,
            )
            return await _post(
                session,
                posting,
                context,
                transaction_id,
                owner_id=owner_id,
                owned={account_id},
                link=_link(keyed),
            )

        return await _in_transaction(session, context, attempt, keyed)

    return await audited(session, context, run)


async def transfer(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    source_account_id: uuid.UUID,
    destination_account_id: uuid.UUID,
    amount_minor: int,
    idempotency_key: str | None = None,
) -> PostingResult:
    """Move money from owner_id's account to any customer account in the same currency."""
    _check_key_format(idempotency_key)
    context = AuditContext(
        AuditAction.POSTING_TRANSFER,
        owner_id,
        {
            "account_id": source_account_id,
            "destination_account_id": destination_account_id,
            "amount_minor": _amount_detail(amount_minor),
        },
        idempotency_key=idempotency_key,
    )

    async def run() -> PostingResult:
        posting = transfer_posting(
            source_account_id=source_account_id,
            destination_account_id=destination_account_id,
            amount_minor=amount_minor,
        )
        keyed = _keyed_request(
            idempotency_key,
            operation=TransactionKind.TRANSFER,
            owner_id=owner_id,
            account_id=source_account_id,
            destination_account_id=destination_account_id,
            amount_minor=amount_minor,
        )

        async def attempt(transaction_id: uuid.UUID) -> PostingResult:
            return await _post(
                session,
                posting,
                context,
                transaction_id,
                owner_id=owner_id,
                owned={source_account_id},
                counterparty=destination_account_id,
                link=_link(keyed),
            )

        return await _in_transaction(session, context, attempt, keyed)

    return await audited(session, context, run)


def _amount_detail(amount_minor: object) -> DetailValue:
    # The requested amount, for the audit trail. Only a real int is recorded: anything else is
    # invalid_amount, and must not turn into a second error while building the audit record.
    return amount_minor if type(amount_minor) is int else None


def _check_key_format(idempotency_key: str | None) -> None:
    # The API validates the header first; anything else is a caller's programming error, and
    # must not reach an audit record (whose key column has its own CHECK).
    if idempotency_key is not None and not is_valid_idempotency_key(idempotency_key):
        raise ValueError("an idempotency key must be 1-255 visible ASCII characters")


def _keyed_request(
    idempotency_key: str | None,
    *,
    operation: TransactionKind,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    amount_minor: int,
    destination_account_id: uuid.UUID | None = None,
) -> _KeyedRequest | None:
    """The key and fingerprint of a pre-validated request, or None without a key."""
    if idempotency_key is None:
        return None
    fingerprint = request_fingerprint(
        operation=operation,
        user_id=owner_id,
        account_id=account_id,
        destination_account_id=destination_account_id,
        amount_minor=amount_minor,
    )
    return _KeyedRequest(owner_id, account_id, idempotency_key, fingerprint)


def _link(keyed: _KeyedRequest | None) -> tuple[uuid.UUID, str] | None:
    """The (user_id, key) a keyed posting's header names (migration 0011), or None."""
    return None if keyed is None else (keyed.owner_id, keyed.key)


async def _in_transaction(
    session: AsyncSession,
    context: AuditContext,
    attempt: Callable[[uuid.UUID], Awaitable[PostingResult]],
    keyed: _KeyedRequest | None,
) -> PostingResult:
    """Run `attempt` in its own transaction, retrying the whole of it on transient failures.

    The idempotency key, if any, is claimed first in every attempt, with that attempt's own
    transaction id: a rolled-back attempt takes its claim with it.
    """
    for number in range(1, MAX_ATTEMPTS + 1):
        context.attempted_transaction_id = None  # set by _post once this attempt's header exists
        transaction_id = uuid.uuid7()
        committing = False
        try:
            async with session.begin():
                result = None
                if keyed is not None:
                    result = await _claim_or_replay(session, keyed, transaction_id)
                if result is None:
                    result = await attempt(transaction_id)
                committing = True  # from here on, only the COMMIT itself can fail
                return result
        except Exception as error:
            if committing and not _definitely_rolled_back(error):
                raise CommitOutcomeUnknownError from error
            if not isinstance(error, DBAPIError):
                raise
            # session.begin() has rolled back: nothing of this attempt was committed.
            code = sqlstate(error)
            if code in CONTENTION_SQLSTATES:
                raise TemporarilyUnavailableError from error
            if code not in RETRYABLE_SQLSTATES:
                raise
            if number == MAX_ATTEMPTS:
                logger.warning("posting failed after %d attempts (SQLSTATE %s)", number, code)
                raise TemporarilyUnavailableError from error
            logger.info("retrying posting after SQLSTATE %s (attempt %d)", code, number)
            # Jitter, so that the transactions that collided do not collide again in lockstep.
            await asyncio.sleep(random.uniform(*RETRY_BACKOFF_SECONDS) * number)  # noqa: S311
    raise AssertionError("unreachable: the last attempt returns or raises")


def _definitely_rolled_back(error: Exception) -> bool:
    """Whether an error raised by COMMIT proves that nothing was committed.

    Only if PostgreSQL itself answered with an ERROR (not FATAL or PANIC) on a connection that
    is still open: the server then aborted the transaction, and the session goes on. That holds
    whatever the SQLSTATE (a deferred check, a permission or data error, a deadlock), except
    the codes that themselves mean "outcome unknown". Everything else leaves the outcome
    unknown: an invalidated connection, a client-side driver or network error (no server
    severity, even when it carries a class-level SQLSTATE such as 08003), a FATAL or PANIC
    (the session ended, possibly after the commit), and anything that is not a DBAPIError.
    """
    if not isinstance(error, DBAPIError) or error.connection_invalidated:
        return False
    if server_severity(error) != "ERROR":
        return False
    code = sqlstate(error)
    return (
        code is not None
        and code not in UNKNOWN_OUTCOME_SQLSTATES
        and not code.startswith(CONNECTION_EXCEPTION_CLASS)
    )


async def _claim_or_replay(
    session: AsyncSession, keyed: _KeyedRequest, transaction_id: uuid.UUID
) -> PostingResult | None:
    """Claim the key for `transaction_id` (None: go ahead and post), or rebuild the result of
    the committed posting that already consumed it."""
    try:
        stored = await claim_idempotency_key(
            session,
            user_id=keyed.owner_id,
            key=keyed.key,
            request_fingerprint=keyed.fingerprint,
            transaction_id=transaction_id,
        )
    except IntegrityError as error:
        # Only the known rule is translated; any other violation is a bug and propagates.
        if violated_constraint(error) == KEY_OWNER_MUST_EXIST:
            raise AccountNotFoundError from error  # indistinguishable, as ever (ADR 0009)
        raise
    if stored is None:
        return None
    if stored.request_fingerprint != keyed.fingerprint:
        raise IdempotencyKeyMismatchError

    entry = await find_posted_entry(
        session, transaction_id=stored.transaction_id, account_id=keyed.account_id
    )
    if entry is None:
        # Same fingerprint, so the same account: its entry must exist. Fail closed.
        raise RuntimeError("an idempotency key references a transaction without the entry")
    # Replays are not audited (ADR 0007): nothing happened. Ids only; the key stays out of logs.
    logger.info(
        "idempotent replay of transaction %s for user %s", entry.transaction_id, keyed.owner_id
    )
    return PostingResult(
        transaction_id=entry.transaction_id,
        kind=entry.kind,
        created_at=entry.created_at,
        balances={keyed.account_id: entry.balance_after_minor},
        replayed=True,
    )


async def _settlement_account_of(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    settlement_id = await find_settlement_account_id(session, account_id=account_id)
    if settlement_id is None:
        raise AccountNotFoundError
    return settlement_id


async def _post(
    session: AsyncSession,
    posting: Posting,
    context: AuditContext,
    transaction_id: uuid.UUID,
    *,
    owner_id: uuid.UUID,
    owned: Collection[uuid.UUID],
    counterparty: uuid.UUID | None = None,
    link: tuple[uuid.UUID, str] | None = None,
) -> PostingResult:
    locked = await lock_accounts(session, posting.account_ids)
    _validate(posting, locked, owner_id=owner_id, owned=owned, counterparty=counterparty)

    # Every line's account is locked and validated: only now may anything be written.
    ledger_id = locked[posting.lines[0].account_id].ledger_id
    # A keyed header names its key, which already references transaction_id (the claim).
    transaction = await insert_posting(
        session, posting, ledger_id=ledger_id, transaction_id=transaction_id, idempotency_key=link
    )
    context.attempted_transaction_id = transaction.id
    balances = await apply_balance_deltas(
        session, {line.account_id: line.amount_minor for line in posting.lines}
    )
    # Last write: the success event commits with the posting, or not at all (ADR 0011).
    await record_in_transaction(session, context.succeeded(transaction_id=transaction.id))
    return PostingResult(
        transaction_id=transaction.id,
        kind=posting.kind,
        created_at=transaction.created_at,
        balances={account_id: balances[account_id] for account_id in owned},
    )


def _validate(
    posting: Posting,
    locked: Mapping[uuid.UUID, LockedAccount],
    *,
    owner_id: uuid.UUID,
    owned: Collection[uuid.UUID],
    counterparty: uuid.UUID | None,
) -> None:
    """Business rules, checked against locked (current) rows. Order matters: ownership is
    established before anything else about an account is revealed."""
    for account_id in owned:
        account = locked.get(account_id)
        # Missing, someone else's, or a system account: indistinguishable (ADR 0009).
        if account is None or account.kind != AccountKind.CUSTOMER or account.user_id != owner_id:
            raise AccountNotFoundError

    if counterparty is not None:
        account = locked.get(counterparty)
        if account is None or account.kind != AccountKind.CUSTOMER:
            raise DestinationAccountNotFoundError

    for line in posting.lines:
        if line.account_id not in locked:
            # Owned and counterparty accounts were checked above; anything else is the
            # settlement account, resolved inside this transaction. Missing means a bug.
            raise RuntimeError("posting references an account that was not locked")

    if len({account.ledger_id for account in locked.values()}) != 1:
        raise CurrencyMismatchError

    for line in posting.lines:
        account = locked[line.account_id]
        # Customer balances may not go negative; the settlement account may (ADR 0003).
        if account.kind == AccountKind.CUSTOMER and account.balance_minor + line.amount_minor < 0:
            raise InsufficientFundsError
