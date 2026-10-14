# Backend Engineering Portfolio

Production-oriented Python backend projects focused on fintech, banking systems, data integrity, and secure API design.

## Projects

| Project | Status | Description |
|---|---|---|
| [banking-ledger-api](banking-ledger-api/) | Completed | Double-entry banking ledger API built with FastAPI, PostgreSQL, and SQLAlchemy. Features ACID transactions, concurrency control, idempotent operations, authentication, audit logging, and least-privilege database roles. |
| banking-transaction-analytics | Planned | Transaction ingestion, validation, and analytics pipeline. |

## Featured Project

### Banking Ledger API

A backend service that models core banking operations with an emphasis on correctness, data integrity, and security.

**Key engineering areas**
- Double-entry bookkeeping and ledger integrity.
- Transactional database operations and concurrency control.
- Idempotency and safe request retries.
- Authentication, authorization, and auditability.
- PostgreSQL privilege separation and least-privilege access.
- Integration testing, database migrations, and CI/CD.
- Guarded database backup and restore rehearsal.

**Tech stack:** Python, FastAPI, PostgreSQL, SQLAlchemy 2.0, Alembic, pytest, Docker Compose, GitHub Actions.

**Repository:** [banking-ledger-api](banking-ledger-api/)

See the project README for architecture, setup instructions, testing, security decisions, and known limitations.

## Engineering Principles

- Correctness and data integrity over superficial feature count.
- Explicit transaction boundaries and predictable failure handling.
- Least privilege and defense in depth.
- Automated testing and reproducible development workflows.
- Documented architectural decisions and transparent trade-offs.

## Data

All data used in these projects is synthetic.

## License

[MIT](LICENSE)
