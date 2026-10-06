.DEFAULT_GOAL := help
.PHONY: help install up down logs test lint fmt typecheck check

# Load the local .env (if present) and export it, so host-side tools (pytest, alembic) see the
# same credentials as Docker Compose. DATABASE_URL is derived from them rather than duplicated;
# an explicitly set DATABASE_URL (e.g. in CI) wins.
-include .env
export
DATABASE_URL ?= postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@127.0.0.1:$(POSTGRES_PORT)/$(POSTGRES_DB)

help: ## List available targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Create the local venv from uv.lock
	uv sync --frozen

up: ## Build and start the stack in the background
	docker compose up -d --build

down: ## Stop the stack (data volume is kept)
	docker compose down

logs: ## Follow container logs
	docker compose logs -f

test: ## Run the test suite
	uv run pytest

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-fix lint issues and format code
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## Static type checking (mypy --strict)
	uv run mypy

check: lint typecheck test ## Run every quality gate
