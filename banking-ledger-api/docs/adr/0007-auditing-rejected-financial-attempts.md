# ADR 0007: Auditing rejected financial attempts outside the financial transaction

- Status: Accepted
- Date: 2026-10-05

## Context

Audit events live in an append-only `audit_events` table. An audit row written inside the
posting transaction is atomic with it. That is exactly right for successes, but when a posting
is rejected the rollback would erase the evidence of the attempt.

## Decision

- **Success**: the `succeeded` audit row is inserted inside the financial transaction. A
  posted transaction and its success audit therefore commit together or not at all.
- **Rejection or failure**:
  1. The financial transaction rolls back completely.
  2. Its session is closed and the connection goes back to the pool.
  3. Only then does `record_outside_transaction()` open a **fresh session** and insert a
     `rejected` (business rule) or `failed` (system error) row in its own short transaction.
- The audit writer **never raises**. If its insert fails, it logs an ERROR with the full event
  (the log becomes the fallback sink) and the caller returns the original error unchanged.
  An audit failure can never change the financial or API outcome.
- Deadlock retries are logged. Only the final outcome is audited.

What gets a database audit row:

| Event | Audit row |
|---|---|
| Posting succeeded | `succeeded` |
| Business rejection (insufficient funds, inactive account, currency mismatch) | `rejected` with a reason code |
| Debit from an account the caller does not own | `rejected`, `account_not_accessible` |
| Idempotency key reused with a different body | `rejected`, `idempotency_key_mismatch` |
| System error | `failed` |
| Idempotent replay, malformed body, unauthenticated request | None, logs only |

## Alternatives considered

- **Savepoint around the posting**: does not cover failures at `COMMIT` (the deferred balance
  trigger), connection loss or crashes. It also keeps locks held while auditing.
- **Autonomous transaction through `dblink`**: an extension plus hidden connections.
- **Writing an "attempted" event first, in its own transaction**: the strongest guarantee, but
  an extra round-trip on every request. This is the documented upgrade path.

## Consequences and deliberate MVP trade-off

- Financial atomicity is unaffected. The audit write happens after the financial transaction
  has ended.
- Successful postings are audited exactly once.
- Rejected and failed attempts are audited **best-effort**. If the process crashes between the
  rollback and the audit commit, the database audit row is lost. Only the log line
  (if already emitted) remains. This is accepted because no money moved; the loss is in
  observability, not in ledger correctness.
- The audit session is opened only after the main session is released. Acquiring a second
  connection while holding the first can deadlock the pool under load.
