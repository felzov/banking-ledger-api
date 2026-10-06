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
