.DEFAULT_GOAL := help
.PHONY: help install up down logs bootstrap migrate revision db-shell test smoke lint fmt typecheck check

# Load the local .env (if present) and export it, so host-side tools see the same values as
# Docker Compose. Each role's URL is derived from them, never exported, and handed to the one
# recipe that needs it (ADR 0015): the admin's to the bootstrap, the owner's to the migrations.
-include .env
export
ADMIN_DATABASE_URL = postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@127.0.0.1:$(POSTGRES_PORT)/$(POSTGRES_DB)
OWNER_DATABASE_URL = postgresql+asyncpg://ledger_owner:$(LEDGER_OWNER_PASSWORD)@127.0.0.1:$(POSTGRES_PORT)/$(POSTGRES_DB)
unexport ADMIN_DATABASE_URL OWNER_DATABASE_URL TEST_DATABASE_URL

help: ## List available targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Create the local venv from uv.lock
	uv sync --frozen

up: ## Build and start the stack (db, bootstrap, migrate, api, in that order); waits until healthy
	docker compose up -d --build --wait

down: ## Stop the stack (data volume is kept)
	docker compose down

logs: ## Follow container logs
	docker compose logs -f

bootstrap: ## Create or reconcile the database roles, as the admin (the db must be up)
	@# Silent: make would echo the URL, password included.
	@echo '+ python -m ledger_api.admin.bootstrap (as the admin)'
	@ADMIN_DATABASE_URL='$(ADMIN_DATABASE_URL)' uv run python -m ledger_api.admin.bootstrap

migrate: ## Apply migrations as ledger_owner (after `make bootstrap`; the db must be up)
	@echo '+ alembic upgrade head (as ledger_owner)'
	@DATABASE_URL='$(OWNER_DATABASE_URL)' uv run alembic upgrade head

revision: ## Create an empty migration: make revision m="add something"
	@test -n "$(m)" || (echo 'usage: make revision m="description"' && exit 1)
	uv run alembic revision -m "$(m)"

db-shell: ## Open psql inside the database container
	docker compose exec db psql -U $(POSTGRES_USER) -d $(POSTGRES_DB)

# The test suite runs against its own disposable cluster (compose service test-db, data on tmpfs),
# never the development database: this always overrides DATABASE_URL.
POSTGRES_TEST_PORT ?= 5433
TEST_DATABASE_URL = postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@127.0.0.1:$(POSTGRES_TEST_PORT)/$(POSTGRES_DB)
# The rate-limit tests' Redis (compose service test-redis). TEST_REDIS_URL, never REDIS_URL: the
# app reads REDIS_URL, and the rest of the suite must run without limits.
REDIS_TEST_PORT ?= 6380

# Compose interpolates the whole file, so the role passwords other services require must be set
# even though test-db uses none of them: placeholders when .env has none, never passed to test-db.
TEST_COMPOSE = LEDGER_OWNER_PASSWORD="$${LEDGER_OWNER_PASSWORD:-unused-by-test-db}" \
	LEDGER_APP_PASSWORD="$${LEDGER_APP_PASSWORD:-unused-by-test-db}" docker compose --profile test

test: ## Run the test suite against the disposable test cluster and Redis (started if needed)
	$(TEST_COMPOSE) up -d --wait test-db test-redis
	@# Mark the test-db server itself, addressed by its service name (never through DATABASE_URL):
	@# tests/database.py refuses any server without this marker. Data on tmpfs: the marker goes
	@# with the container.
	$(TEST_COMPOSE) exec -T test-db psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) \
		-v ON_ERROR_STOP=1 -q -c "ALTER SYSTEM SET ledger.test_cluster = 'disposable'" \
		-c "SELECT pg_reload_conf()" -o /dev/null
	@echo '+ pytest (against test-db, 127.0.0.1:$(POSTGRES_TEST_PORT), and test-redis, :$(REDIS_TEST_PORT))'
	@DATABASE_URL='$(TEST_DATABASE_URL)' LEDGER_TEST_CLUSTER=disposable \
		TEST_REDIS_URL='redis://127.0.0.1:$(REDIS_TEST_PORT)/0' uv run pytest

# The start-up smoke test (scripts/compose-smoke.sh) in an isolated compose project: its own name,
# volume, image tag and host ports, configured from .env.example only (a clean environment, so
# nothing from the local .env leaks in). Afterwards it removes that project's containers, volume
# and image, and nothing else: the development stack and its volume are never touched.
SMOKE_PROJECT = banking-ledger-smoke
SMOKE_IMAGE = banking-ledger-api:smoke
SMOKE_ENV = env -i PATH="$$PATH" HOME="$$HOME" COMPOSE_PROJECT_NAME=$(SMOKE_PROJECT) \
	LEDGER_IMAGE=$(SMOKE_IMAGE) COMPOSE_ENV_FILE=.env.example POSTGRES_PORT=55432 API_PORT=58000

smoke: ## Start-up smoke test in an isolated compose project (own volume, ports, image)
	@$(SMOKE_ENV) SMOKE_UP_ARGS=--build scripts/compose-smoke.sh; status=$$?; \
	$(SMOKE_ENV) docker compose --env-file .env.example down --volumes --remove-orphans; \
	docker image rm --force $(SMOKE_IMAGE) >/dev/null 2>&1; \
	exit $$status

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-fix lint issues and format code
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## Static type checking (mypy --strict)
	uv run mypy

check: lint typecheck test ## Run every quality gate
