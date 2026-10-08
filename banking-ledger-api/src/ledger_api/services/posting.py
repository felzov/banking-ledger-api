"""Money movement: deposits, withdrawals and transfers (ADR 0010).

Lifecycle of every posting, inside ONE database transaction:

  1. pre-validate (pure, before BEGIN): amount, distinct accounts
  2. BEGIN
  3. resolve immutable facts (the settlement account of an account's ledger)
  4. lock every account of the posting: one statement, ascending id, FOR UPDATE OF accounts
  5. validate against the locked rows only: ownership, counterparty, currency, funds
  6. write: transaction header (sealed with entry_count), entries, relative balance updates
  7. COMMIT: deferred triggers check >= 2 entries, sum zero, count = entry_count

Any error rolls back everything, releasing the locks: nothing partial is ever committed.
No I/O other than this session happens between BEGIN and COMMIT.

Transient failures retry the WHOLE transaction (steps 2-7), never a part of it: a deadlock
or serialization failure up to MAX_ATTEMPTS in total, with jittered backoff. A lock or
statement timeout is not retried (the caller already waited); it, and exhausted retries,
become TemporarilyUnavailableError. Retrying is safe because a failed attempt committed
nothing.
"""

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.errors import sqlstate
from ledger_api.data.posting import (
    LockedAccount,
    apply_balance_deltas,
    find_settlement_account_id,
    insert_posting,
    lock_accounts,
)
from ledger_api.domain.account import AccountKind
from ledger_api.domain.errors import (
    AccountNotFoundError,
    CurrencyMismatchError,
    DestinationAccountNotFoundError,
    InsufficientFundsError,
    TemporarilyUnavailableError,
)
from ledger_api.domain.posting import (
    Posting,
    deposit_posting,
    transfer_posting,
    validate_amount,
    withdrawal_posting,
)
from ledger_api.domain.transaction import TransactionKind

logger = logging.getLogger(__name__)

# Attempts per posting, the first one included (ADR 0005). Concurrency tests lower it to 1 so
# that a deadlock cannot be hidden by a retry.
MAX_ATTEMPTS = 3
# deadlock_detected, serialization_failure: the transaction was rolled back; retry it whole.
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
# lock_not_available (lock_timeout), query_canceled (statement_timeout): already waited.
CONTENTION_SQLSTATES = frozenset({"55P03", "57014"})
RETRY_BACKOFF_SECONDS = (0.01, 0.05)


@dataclass(frozen=True, slots=True)
class PostingResult:
    transaction_id: uuid.UUID
    kind: TransactionKind
    created_at: datetime
    # New balances of the caller's own accounts only: never a counterparty's (ADR 0010).
    balances: Mapping[uuid.UUID, int]


async def deposit(
    session: AsyncSession, *, owner_id: uuid.UUID, account_id: uuid.UUID, amount_minor: int
) -> PostingResult:
    """Credit owner_id's account with money arriving from outside (the settlement account)."""
    validate_amount(amount_minor)

    async def attempt() -> PostingResult:
        settlement_id = await _settlement_account_of(session, account_id)
        posting = deposit_posting(
            account_id=account_id, settlement_account_id=settlement_id, amount_minor=amount_minor
        )
        return await _post(session, posting, owner_id=owner_id, owned={account_id})

    return await _in_transaction(session, attempt)


async def withdraw(
    session: AsyncSession, *, owner_id: uuid.UUID, account_id: uuid.UUID, amount_minor: int
) -> PostingResult:
    """Debit owner_id's account with money leaving to outside (the settlement account)."""
    validate_amount(amount_minor)

    async def attempt() -> PostingResult:
        settlement_id = await _settlement_account_of(session, account_id)
        posting = withdrawal_posting(
            account_id=account_id, settlement_account_id=settlement_id, amount_minor=amount_minor
        )
        return await _post(session, posting, owner_id=owner_id, owned={account_id})

    return await _in_transaction(session, attempt)


async def transfer(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    source_account_id: uuid.UUID,
    destination_account_id: uuid.UUID,
    amount_minor: int,
) -> PostingResult:
    """Move money from owner_id's account to any customer account in the same currency."""
    posting = transfer_posting(
        source_account_id=source_account_id,
        destination_account_id=destination_account_id,
        amount_minor=amount_minor,
    )

    async def attempt() -> PostingResult:
        return await _post(
            session,
            posting,
            owner_id=owner_id,
            owned={source_account_id},
            counterparty=destination_account_id,
        )

    return await _in_transaction(session, attempt)


async def _in_transaction(
    session: AsyncSession, attempt: Callable[[], Awaitable[PostingResult]]
) -> PostingResult:
    """Run `attempt` in its own transaction, retrying the whole of it on transient failures."""
    for number in range(1, MAX_ATTEMPTS + 1):
        try:
            async with session.begin():
                return await attempt()
        except DBAPIError as error:
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


async def _settlement_account_of(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    settlement_id = await find_settlement_account_id(session, account_id=account_id)
    if settlement_id is None:
        raise AccountNotFoundError
    return settlement_id


async def _post(
    session: AsyncSession,
    posting: Posting,
    *,
    owner_id: uuid.UUID,
    owned: Collection[uuid.UUID],
    counterparty: uuid.UUID | None = None,
) -> PostingResult:
    locked = await lock_accounts(session, posting.account_ids)
    _validate(posting, locked, owner_id=owner_id, owned=owned, counterparty=counterparty)

    # Every line's account is locked and validated: only now may anything be written.
    ledger_id = locked[posting.lines[0].account_id].ledger_id
    transaction = await insert_posting(session, posting, ledger_id=ledger_id)
    balances = await apply_balance_deltas(
        session, {line.account_id: line.amount_minor for line in posting.lines}
    )
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
