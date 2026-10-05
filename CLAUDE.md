# CLAUDE.md

## Project Context

This repository contains portfolio projects designed to demonstrate production-oriented Python backend engineering skills for competitive software engineering internships, with particular relevance to fintech and banking systems.

The projects should demonstrate engineering judgment, not simply CRUD implementation.

Core development philosophy:

Understand → Plan → Delegate → Review → Test → Debug → Refactor → Ship.

Claude Code is an engineering assistant, not an autonomous developer. Do not make large architectural or destructive changes without approval.

---

## Primary Stack

Use the following technologies unless there is a concrete engineering reason not to:

- Python 3.12+
- FastAPI
- asyncio
- Pydantic v2
- SQLAlchemy 2.0
- PostgreSQL
- Alembic
- Redis
- Docker / Docker Compose
- pytest
- pytest-asyncio
- httpx
- Git / GitHub

Potential later technologies:

- Kafka
- Airflow
- Kubernetes
- Prometheus
- OpenTelemetry

Do not introduce additional technologies merely to make the project look impressive. Every technology must solve a real problem.

---

# Project 1 — banking-ledger-api

## Goal

Build a production-oriented banking backend demonstrating:

- Python backend engineering
- FastAPI
- PostgreSQL
- SQLAlchemy
- database transactions
- ACID guarantees
- double-entry bookkeeping
- concurrency control
- idempotency
- authentication and authorization
- validation
- auditability
- testing
- Docker
- API design
- security
- observability

This must NOT become a generic CRUD application.

---

## Core Domain

The initial domain consists of:

User
Account
Ledger
Transaction
TransactionEntry
IdempotencyKey

Expected relationship:

User
  ↓
Account
  ↓
Ledger / Transactions
  ↓
Transaction Entries

A transaction must contain balanced entries.

Example:

Transfer £100 from Account A to Account B:

Account A: -100
Account B: +100

Total:

-100 + 100 = 0

The database must enforce appropriate invariants wherever practical.

---

## Money Representation

Do not use floating-point numbers for monetary values.

Evaluate:

1. integer minor units
2. Decimal

Choose the approach based on correctness, database compatibility, API design and maintainability.

Explain the trade-offs before implementation.

---

## Financial Rules

Financial operations must be atomic.

A transfer must either:

- completely succeed

or:

- completely fail and rollback.

Never allow a partially completed financial transaction.

Important concepts to consider:

- database transactions
- isolation levels
- row locking
- race conditions
- concurrent transfers
- deadlocks
- idempotency
- transaction states
- audit trails
- database constraints

Never silently mutate historical financial records.

Prefer immutable ledger entries.

---

## Expected API

Initial MVP should consider:

POST /users

POST /accounts

GET /accounts/{id}

GET /accounts/{id}/balance

POST /transactions

GET /accounts/{id}/transactions

GET /accounts/{id}/statement

Exact API design must be reviewed before implementation.

---

## Security

Consider:

- authentication
- authorization
- password hashing
- secret management
- input validation
- SQL injection prevention
- sensitive data handling
- rate limiting
- secure error responses
- logging without leaking secrets
- transaction authorization
- idempotency abuse
- replay attacks

Never use real financial information, credentials, API keys or personal data.

All project data must be synthetic.

---

# Project 2 — banking-transaction-analytics

After Project 1 is completed, build a separate data-processing/backend project.

Architecture concept:

Source
→ Ingestion
→ Validation
→ Normalization
→ PostgreSQL
→ Async processing
→ Redis
→ Analytics
→ REST API

Potential functionality:

- CSV/API ingestion
- validation
- normalization
- deduplication
- idempotent processing
- background jobs
- retries
- failed jobs
- processing status
- transaction categorization
- monthly spending analytics
- reporting endpoints
- caching

Kafka/Airflow/Kubernetes may be introduced later only when they solve a concrete architectural problem.

Do not add technologies just for CV keyword stuffing.

---

# Development Workflow

For every non-trivial task:

1. Inspect the relevant code.
2. Identify existing patterns.
3. Understand dependencies.
4. Create a plan.
5. Identify assumptions and risks.
6. Ask for approval when the change is substantial.
7. Implement in small increments.
8. Run tests.
9. Review the diff.
10. Refactor if necessary.
11. Update documentation.
12. Commit meaningful changes.

Never blindly rewrite large portions of the codebase.

Prefer small, reviewable changes.

---

# Claude Code Behavior

Before substantial implementation:

- inspect the repository
- inspect relevant files
- explain the proposed approach
- identify risks
- wait for approval when architectural decisions are involved

Do not:

- delete working code without justification
- introduce unnecessary dependencies
- modify unrelated files
- generate fake implementation
- skip tests
- hide errors
- silently change architecture

If requirements are ambiguous, state the ambiguity and propose options.

---

# Educational Mode

The purpose of these projects is also to learn software engineering.

When explaining an important concept, use:

What?
Why?
How?
Example?
Application in this project?
Common mistakes?

Important concepts to teach during development:

- Python async programming
- FastAPI dependency injection
- Pydantic
- SQLAlchemy 2.0
- PostgreSQL
- database transactions
- isolation levels
- locking
- concurrency
- idempotency
- REST API design
- testing
- Docker
- Redis
- Git
- security
- architecture
- distributed systems

Do not explain trivial syntax unnecessarily.

Focus explanations on engineering decisions.

---

# Testing

Testing is a first-class part of development.

Use:

- unit tests
- integration tests
- API tests
- database tests
- concurrency tests
- idempotency tests

Important financial invariants should have explicit tests.

Examples:

- balances cannot become inconsistent
- double-entry transactions balance to zero
- failed transfers rollback completely
- repeated idempotent requests do not duplicate transactions
- unauthorized users cannot access another user's account
- concurrent operations preserve consistency

Do not simply maximize test coverage percentage.

Tests should protect meaningful behavior.

---

# Database

Use PostgreSQL for the main application database.

Use Alembic for migrations.

Database constraints should enforce important invariants where practical.

Avoid relying exclusively on application-level validation for financial invariants.

Consider:

- foreign keys
- unique constraints
- check constraints
- indexes
- timestamps
- transaction boundaries
- locking
- isolation

---

# Docker

The development environment should be reproducible.

Prefer Docker Compose for local infrastructure.

The application should be runnable with documented commands.

Do not add Kubernetes until the project actually benefits from it.

---

# Code Quality

Prefer:

- clear names
- small functions
- explicit dependencies
- strong typing
- clear domain boundaries
- maintainable modules
- minimal duplication

Avoid:

- premature abstractions
- unnecessary design patterns
- huge service classes
- deeply nested logic
- magic values
- global mutable state

Use type hints consistently.

---

# Git

Commits should represent meaningful development steps.

Examples:

chore: initialize project

chore: configure Docker development environment

feat: add database configuration

feat: add user domain model

feat: add account domain model

feat: add ledger models

feat: add transaction service

feat: implement transaction idempotency

test: add ledger integration tests

test: add transaction concurrency tests

refactor: separate domain and infrastructure layers

docs: document system architecture

Commits must represent actual work performed.

Do not fabricate development history or timestamps to mislead recruiters or other reviewers.

---

# Documentation

Maintain useful documentation.

The README should eventually include:

- project purpose
- architecture
- technology stack
- local setup
- environment variables
- database setup
- API documentation
- testing
- architecture decisions
- important design trade-offs
- example requests
- limitations
- future improvements

Document important architectural decisions.

---

# Context Engineering

Keep the context useful.

Prefer:

- concise task descriptions
- relevant file inspection
- focused changes
- explicit constraints
- clear acceptance criteria

Avoid dumping unnecessary information into prompts.

Before large tasks, establish:

Goal
Constraints
Current state
Expected result
Acceptance criteria

---

# Prompt Engineering

For complex tasks, structure prompts around:

Context
Goal
Constraints
Requirements
Acceptance criteria
Verification

Do not ask Claude to "make everything better".

Define exactly what should change.

---

# Financial Domain Principles

Correctness is more important than convenience.

For financial operations:

- never use floating point for money
- never partially apply a transfer
- never silently overwrite historical ledger data
- preserve auditability
- enforce authorization
- design for retries
- consider concurrent requests
- use idempotency for operations where appropriate
- test failure scenarios

---

# Security Principles

Treat security as part of implementation, not as a final checklist.

Consider:

- authentication
- authorization
- secrets
- dependency security
- validation
- database security
- rate limiting
- logging
- error handling
- sensitive information exposure

Never commit secrets.

Use environment variables or appropriate secret-management mechanisms.

---

# Project Development Strategy

Build Project 1 in phases.

Phase 1:
Repository and development environment.

Phase 2:
Database and domain models.

Phase 3:
Users and accounts.

Phase 4:
Ledger and transaction model.

Phase 5:
Money transfers and database transaction boundaries.

Phase 6:
Idempotency and concurrency.

Phase 7:
REST API.

Phase 8:
Authentication and authorization.

Phase 9:
Testing and reliability.

Phase 10:
Security, observability and documentation.

Only after Project 1 is stable should Project 2 begin.

---

# Important Rule

Build fast, learn deeply.

Claude Code may implement code quickly, but the user must understand:

- architecture
- important code paths
- database design
- transaction behavior
- concurrency
- tests
- security decisions

The goal is not merely to produce GitHub repositories.

The goal is to become capable of explaining, debugging, modifying and defending the systems during a technical interview.

---

# First Task

When starting Project 1:

1. Inspect the repository.
2. Read this CLAUDE.md completely.
3. Propose architecture.
4. Propose domain model.
5. Propose database schema.
6. Propose API design.
7. Propose repository structure.
8. Propose testing strategy.
9. Propose Docker setup.
10. Identify risks and open questions.

Do NOT implement substantial code until the user approves the plan.
