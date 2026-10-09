# ADR 0004: Immutable transactions without a status column

- Status: Accepted
- Date: 2026-10-05

## Context

Historical financial records must never be silently changed. Transaction states such as
pending, posted or failed only matter when there are holds or asynchronous external payment
rails. The MVP has neither.

## Decision

- A row in `transactions` exists only after it has been posted successfully. There is no
  `status` column.
- `transactions`, `ledger_entries` and `audit_events` are append-only. A
  `BEFORE UPDATE OR DELETE` trigger raises. The application's database role will not be
  granted `UPDATE` or `DELETE` on these tables.
- Rejected and failed attempts are recorded in `audit_events` (see ADR 0007), not as
  transactions.
- Corrections will be new reversing transactions (post-MVP), never edits.

## Alternatives considered

- **A status enum with one reachable value today**: adds complexity that nothing uses and
  invites mutation of posted rows.

## Consequences

- Simple invariant: everything in `transactions` is final.
- Adding holds or two-phase transfers later will need a separate model (for example
  `holds`), not mutable transaction rows.

## Amendment (Phase 5, 2026-10-10)

- `BEFORE TRUNCATE` statement triggers now protect `transactions`, `ledger_entries` and
  `audit_events` (migrations 0003 and 0004). Before, TRUNCATE fired no row trigger, and
  `TRUNCATE ledger_entries` erased the ledger without an error.
- **The grant model above is not implemented yet.** The application connects as the PostgreSQL
  superuser created by the Compose bootstrap. Triggers bind ordinary sessions only: a superuser
  or the table owner can disable or drop them, or skip them with
  `session_replication_role = replica`. Append-only is therefore a guarantee against
  application bugs and mistakes, **not against that role**. Least-privilege roles (a migration
  owner; an application role with `INSERT`/`SELECT` only on these tables, no `TRUNCATE`) are
  future hardening work (Phase 10). Reconciliation re-checks the invariants independently
  (ADR 0012).
