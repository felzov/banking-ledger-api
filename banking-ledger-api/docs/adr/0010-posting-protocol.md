# ADR 0010: Posting protocol

- Status: Accepted
- Date: 2026-10-08

## Context

Phase 4 moves money: deposits and withdrawals against the per-currency settlement account,
and transfers between customer accounts (ADR 0003). A posting reads balances, decides, and
writes a transaction, its entries and new balances. It must be atomic, must not lose updates
or overdraw under concurrency, must not deadlock, and must keep the database invariants
authoritative.

Two problems were confirmed experimentally before implementation:

- **Append gap.** The Phase 2 COMMIT-time checks (>= 2 entries, sum zero) still allowed a
  *balanced* pair of entries to be appended to an already-committed transaction.
- **Locking pitfalls.** Opposite lock orders deadlock (40P01). A `FOR UPDATE` on a query that
  joins `ledgers` also locks the ledger row, which would serialize every posting in a
  currency.

## Decision

### Sealed transactions

`transactions.entry_count` (immutable, `>= 2`) declares how many entries the transaction was
posted with. The deferred trigger additionally requires the actual count to equal it
(`ck_transactions_entry_count`), so any later append, balanced or not, fails at COMMIT.

### Lifecycle of a posting (one database transaction)

1. **Pre-validate** (pure, before BEGIN): the amount is an `int` in `1..MAX_AMOUNT_MINOR`
   (100,000,000 minor units); transfer source and destination differ.
2. **BEGIN.**
3. **Resolve immutable facts only:** the settlement account of a customer account's ledger.
4. **Lock every account of the posting in one statement**:
   `SELECT ... FROM accounts WHERE id IN (...) ORDER BY id FOR UPDATE OF accounts`.
   One global lock order (ascending id) means postings cannot deadlock with each other.
   `OF accounts` means no other table's rows are ever locked. The settlement account is
   locked too, so deposits and withdrawals in one currency serialize on it (accepted in
   ADR 0003).
5. **Validate against the locked rows only**, in this order: ownership (missing, foreign or
   system account: `account_not_found`), transfer destination
   (`destination_account_not_found`), one ledger (`currency_mismatch`), funds
   (`insufficient_funds`).
6. **Write:** the sealed header, the entries, and relative balance updates
   (`balance_minor = balance_minor + :delta ... RETURNING`).
7. **COMMIT:** the deferred triggers check >= 2 entries, sum zero, and count = entry_count.

Rules that make this correct:

- **Mutable state is read only from the locking statement**, never before it.
- **Locked state is read as Core rows, not ORM entities.** An `Account` already in the
  session's identity map keeps its stale balance even after a fresh `SELECT ... FOR UPDATE`.
- **No I/O other than this session between BEGIN and COMMIT.**
- **A result reveals only the caller's own balances**, never a counterparty's.

### Failures

Any error at any step rolls back the whole transaction, including balance updates already
made; nothing partial is committed. Errors that indicate a bug (an untranslated
`IntegrityError` from the CHECK constraint or a deferred trigger) are not disguised as
business errors.

| Condition | SQLSTATE | Handling |
|---|---|---|
| Deadlock, serialization failure | 40P01, 40001 | Retry the **whole** transaction, at most 3 attempts in total, jittered backoff |
| Lock timeout (2 s), statement timeout (5 s) | 55P03, 57014 | Not retried |
| Retries exhausted, or a timeout | | `temporarily_unavailable` (503) |
| Business rejection | | `insufficient_funds`, `currency_mismatch`, `invalid_amount`, `same_account_transfer` (422); not found (404); never retried |

Retrying is safe because a failed attempt committed nothing. The concurrency tests run with
retries disabled, so they prove the lock order prevents deadlocks rather than hiding them.

### Scope

The posting engine is service-only in Phase 4. HTTP endpoints that move money come in
Phase 6, together with `Idempotency-Key` (ADR 0006): without it, a client retry after a
timeout would post twice.

## Alternatives considered

- **Detecting appends via `xmin` against `pg_current_xact_id()`**: breaks under savepoints
  and is fragile around transaction-id wraparound.
- **A "sealed" flag set after the entries**: needs an UPDATE of an immutable row.
- **A `SECURITY DEFINER` posting function and no direct INSERT privilege**: strong, but a
  privilege model (Phase 10); `entry_count` remains valid alongside it.
- **Locking rows one at a time, or in posting order**: deadlocks under opposite transfers
  (demonstrated by the tests: 37 of 40 opposite transfers deadlocked).
- **`FOR NO KEY UPDATE`**: slightly less blocking for foreign-key checks from other
  transactions, but every writer locks first anyway; `FOR UPDATE` (ADR 0005) is kept.
- **A trigger keeping `balance_minor` in step with the entries**: would hide the balance
  logic; the projection stays application-maintained and is verified by reconciliation.

## Consequences

- Phase 5 adds the success audit row as the last write before COMMIT, and rejection audits
  after the rollback (ADR 0007). Phase 6 adds the idempotency key as the first write after
  BEGIN (ADR 0006).
- **Known limitation:** if the connection drops *during* COMMIT, the client cannot know
  whether the posting committed. Idempotency keys (Phase 6) make the retry safe.
- `balance_minor` is checked by reconciliation queries (balances against entries; every
  ledger's entries and balances sum to zero), not by a database constraint.
- The settlement account is a hot row; sharded settlement accounts are the documented fix.
