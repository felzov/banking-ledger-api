# ADR 0003: Per-currency ledgers and signed double-entry postings

- Status: Accepted
- Date: 2026-10-05

## Context

Every movement of money must be recorded so that money is never created or destroyed by a bug,
and so that the database itself can reject inconsistent data.

## Decision

- A `ledgers` table with exactly one ledger per currency. GBP and EUR are seeded. Every account
  and every transaction belongs to one ledger.
- Each user may hold at most one account per currency (`UNIQUE (user_id, ledger_id)`).
- Entries live in `ledger_entries` and carry **signed** amounts (`amount_minor`). An account's
  balance is the sum of its entries.
- Invariant per transaction: at least two entries, and `SUM(amount_minor) = 0`. It is enforced
  by the domain code **and** by a deferred PostgreSQL constraint trigger that runs at commit.
- Composite foreign keys `(account_id, ledger_id)` and `(transaction_id, ledger_id)` make it
  impossible for an entry to reference an account in a different currency.
- Deposits and withdrawals are simulated against a per-currency **external settlement** system
  account. It may go negative; customer accounts may not.

| Operation (£100) | Entries |
|---|---|
| Deposit to A | settlement −10000, A +10000 |
| Withdrawal from A | A −10000, settlement +10000 |
| Transfer A → B | A −10000, B +10000 |

- `accounts.balance_minor` is a cached projection. It is updated in the same transaction as
  the entries, under a row lock. Reconciliation checks that
  `balance_minor = SUM(ledger_entries.amount_minor)` and that every ledger sums to zero.

## Alternatives considered

- **Debit/credit direction with positive amounts and a normal balance per account**: closer to
  accounting vocabulary, but the invariant is harder to state and enforce in SQL.
- **Balance always computed with `SUM()`**: always consistent, but every read costs O(entries)
  and a non-negative balance cannot be expressed as a CHECK constraint.

## Consequences

- The settlement account balance always equals minus the total customer money in that
  currency, which gives a free system-wide sanity check.
- No FX: cross-currency transfers are structurally impossible in the MVP.
- Every deposit locks the settlement account, so deposits serialize on it. This is accepted
  for the MVP; sharded settlement accounts are the documented fix.
