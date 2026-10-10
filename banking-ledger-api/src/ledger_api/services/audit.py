"""Audit trail write protocol (ADR 0007, ADR 0011).

- **Succeeded**: record_in_transaction() inside the business transaction, as its last write.
  The event commits with the operation or not at all; if the insert fails, the operation fails.
- **Rejected / failed**: the business transaction rolls back first. Its connection is then back
  in the pool, and record_outside_transaction() writes the event in a NEW transaction on the
  same session, so one operation never holds two connections. Best-effort: the writer never
  raises. If it cannot write, it logs an ERROR and the caller re-raises its original error.

audited() applies the second rule to a whole service call.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import partial

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_api.data.audit import insert_audit_event
from ledger_api.data.errors import sqlstate, violated_constraint
from ledger_api.domain.audit import (
    ATTEMPTED_TRANSACTION_ID,
    INTERNAL_ERROR,
    AuditAction,
    AuditOutcome,
    AuditRecord,
    DetailValue,
)
from ledger_api.domain.errors import DomainError, InternalError, UnavailableError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AuditContext:
    """What one audited service call is about. Details are safe metadata only (ADR 0011)."""

    action: AuditAction
    actor_user_id: uuid.UUID | None
    details: dict[str, DetailValue] = field(default_factory=dict)
    # Postings only: the transaction id of the latest attempt, once its header was flushed.
    attempted_transaction_id: uuid.UUID | None = None

    def succeeded(
        self,
        *,
        actor_user_id: uuid.UUID | None = None,
        transaction_id: uuid.UUID | None = None,
        created: Mapping[str, DetailValue] | None = None,
    ) -> AuditRecord:
        """The success event. `created` adds facts known only once the operation's writes
        exist (e.g. a new account's id): they belong to this event alone, never to the shared
        context, so a later rejection or failure event cannot name a row that was rolled back.
        """
        return AuditRecord(
            self.action,
            AuditOutcome.SUCCEEDED,
            actor_user_id=actor_user_id or self.actor_user_id,
            transaction_id=transaction_id,
            details={**self.details, **(created or {})},
        )

    def rejected(self, error: DomainError) -> AuditRecord:
        return AuditRecord(
            self.action,
            AuditOutcome.REJECTED,
            actor_user_id=self.actor_user_id,
            reason=error.code,
            details=self.details,
        )

    def failed(self, error: Exception) -> AuditRecord:
        reason = error.code if isinstance(error, DomainError) else INTERNAL_ERROR
        details = {**self.details, **_failure_details(error)}
        if self.attempted_transaction_id is not None:
            details[ATTEMPTED_TRANSACTION_ID] = self.attempted_transaction_id
        return AuditRecord(
            self.action,
            AuditOutcome.FAILED,
            actor_user_id=self.actor_user_id,
            reason=reason,
            details=details,
        )


def _failure_details(error: Exception) -> dict[str, DetailValue]:
    """Identifies the failure without its message: messages can quote data."""
    details: dict[str, DetailValue] = {"error_type": type(error).__name__}
    # A TemporarilyUnavailableError wraps the database error that caused it.
    database_error = error.__cause__ if isinstance(error, UnavailableError) else error
    if isinstance(database_error, DBAPIError):
        details["sqlstate"] = sqlstate(database_error)
        if isinstance(database_error, IntegrityError):
            details["constraint"] = violated_constraint(database_error)
        if database_error.connection_invalidated:
            # The connection died: if this happened during COMMIT, the outcome is unknown.
            details["connection_invalidated"] = True
    return details


async def record_in_transaction(session: AsyncSession, record: AuditRecord) -> None:
    """Write a success event inside the caller's transaction. Raises like any other write."""
    await insert_audit_event(session, record)


async def record_outside_transaction(session: AsyncSession, record: AuditRecord) -> None:
    """Write a rejected or failed event in its own short transaction. Never raises."""
    if session.in_transaction():
        # A bug in the caller: writing now would join (and be undone with) the rolled-back
        # work, or hold a second connection. Refuse, loudly, without failing the caller.
        _log_lost(record, "the session is still in a transaction")
        return
    try:
        async with session.begin():
            await insert_audit_event(session, record)
    except Exception as error:
        _log_lost(record, type(error).__name__, error)


def _log_lost(record: AuditRecord, cause: str, error: Exception | None = None) -> None:
    # The log line is the fallback audit sink. The record's fields are safe by construction
    # (stable codes, ids and amounts); nothing from the request body or the database URL.
    logger.error(
        "audit event not written (%s): action=%s outcome=%s reason=%s actor_user_id=%s details=%s",
        cause,
        record.action,
        record.outcome,
        record.reason,
        record.actor_user_id,
        dict(record.details),
        exc_info=error,
    )


async def audited[T](
    session: AsyncSession, context: AuditContext, operation: Callable[[], Awaitable[T]]
) -> T:
    """Run `operation` (which owns its transactions); audit its rejection or failure after the
    fact, then re-raise the ORIGINAL exception, unchanged, whatever happens to the audit write.

    Success is audited by the operation itself, inside its transaction. Cancellation
    (BaseException) is not audited: the request is gone, and the write would be cancelled too.
    """
    try:
        return await operation()
    except DomainError as error:
        if isinstance(error, UnavailableError | InternalError):  # system conditions
            await _record_after_rollback(session, partial(context.failed, error))
        else:
            await _record_after_rollback(session, partial(context.rejected, error))
        raise
    except Exception as error:
        await _record_after_rollback(session, partial(context.failed, error))
        raise


async def _record_after_rollback(session: AsyncSession, build: Callable[[], AuditRecord]) -> None:
    try:
        record = build()
    except Exception:
        # Building the record is pure, but must not be the thing that replaces the original
        # error either (e.g. a detail value of an unexpected type).
        logger.exception("audit event not written: invalid audit record")
        return
    await record_outside_transaction(session, record)
