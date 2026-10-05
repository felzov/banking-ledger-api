# ADR 0005: Pessimistic row locking under READ COMMITTED

- Status: Accepted
- Date: 2026-10-05

## Context

Posting reads balances, checks them, and writes new ones. Without coordination, two
concurrent withdrawals can both see the same balance and overdraw the account.

## Decision

- Use the default READ COMMITTED isolation level. Lock every involved account with
  `SELECT ... FOR UPDATE`, **in ascending id order**, before validating anything.
- Update balances atomically: `balance = balance + :delta`.
- Retry deadlocks (SQLSTATE `40P01`) up to 3 times with jitter.
- Set `lock_timeout` and `statement_timeout` so a stuck lock cannot exhaust the connection pool.
- No network I/O inside a financial transaction.
- `CHECK (kind = 'system' OR balance >= 0)` is the last line of defense if a code path forgets
  to lock.

## Why READ COMMITTED is enough here

When a transaction waits on a row lock, PostgreSQL re-reads the latest committed version of
that row once it gets the lock. The second of two concurrent withdrawals therefore sees the
balance after the first one committed.

## Alternatives considered

- **SERIALIZABLE**: every money-moving call needs a retry loop for SQLSTATE `40001`, and the
  failures are harder to reason about.
- **Optimistic locking with a version column**: scales better under low contention, but needs
  retries everywhere and performs worse on hot accounts.

## Consequences

- Hot accounts serialize. This is acceptable at portfolio scale.
- Lock ordering must be preserved in every new code path. Concurrency tests enforce it.
