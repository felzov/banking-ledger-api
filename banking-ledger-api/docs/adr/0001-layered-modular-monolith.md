# ADR 0001: Layered modular monolith on FastAPI and PostgreSQL

- Status: Accepted
- Date: 2026-10-05

## Context

The service moves money between accounts. Every financial operation must be atomic across
several rows (accounts, transactions, entries, idempotency keys, audit events). The project
must demonstrate production engineering judgement without unnecessary infrastructure.

## Decision

- One deployable FastAPI service and one PostgreSQL database (a modular monolith).
- Strict internal layers:
  - **API**: routers, Pydantic schemas, dependencies. Translates HTTP to service calls only.
  - **Services**: one small module per use case. The only layer that opens, commits or rolls
    back database transactions.
  - **Domain**: pure Python (money, posting rules, domain errors). No I/O.
  - **Data**: SQLAlchemy models and plain async query functions. Never commits.
- Stack: Python 3.14, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async) with asyncpg,
  PostgreSQL 18, Alembic, uv for dependency management, Docker Compose for local
  infrastructure.

## Alternatives considered

- **Microservices**: would turn every transfer into a distributed transaction (sagas or 2PC)
  with no benefit at this scale.
- **Sync SQLAlchemy in FastAPI's threadpool**: simpler and adequate for the load, but the
  project targets the asyncio stack and needs real parallel requests in concurrency tests.

## Consequences

- Real ACID guarantees from a single PostgreSQL transaction.
- Async ORM forbids implicit lazy loading; relationships must be loaded explicitly.
- An `AsyncSession` must never be shared between concurrent tasks: one session per unit of work.
- Layer boundaries are enforced by review, not tooling.

## Amendment (Phase 7, 2026-10-11): every service owns its transaction

Services own their transaction boundaries, reads included: each one runs in
`async with session.begin():` and returns the session outside any transaction, with its
connection back in the pool. Until Phase 7 the read services (`get_user`,
`get_customer_account`, `list_customer_accounts`, `get_statement`) relied on SQLAlchemy's
autobegin and left a transaction open: a write service called next on the same session failed
with "A transaction is already begun", and its failure audit event was lost. Phase 8's
router-level authorization dependency will read on the request session before the route's
service runs, so this had to hold first. A service never calls another service that opens a
transaction; it calls data-layer functions instead. A regression test runs every read service
on a one-connection pool.
