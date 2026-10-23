#!/usr/bin/env bash
# The production start-up sequence, checked from outside the containers (Phase 10, ADR 0015):
#
#     db (healthy) -> bootstrap (admin) -> migrate (ledger_owner) -> api (ledger_app)
#
# The api also needs redis (rate limits, ADR 0016): `compose up --wait` waits for it to be healthy.
# Then an API flow (register, log in, open an account, deposit) and the server's own view: which
# role the API is connected as, who owns the schema, which credentials the API container holds.
#
# Runs against whatever compose project the environment selects: CI's own throwaway stack, or
# `make smoke`'s isolated project (own name, volume, ports and image). Settings:
#   COMPOSE_ENV_FILE  env file for compose (default: compose's own .env lookup)
#   API_PORT          the API's host port (default 8000)
#   SMOKE_UP_ARGS     extra `compose up` arguments, e.g. --no-build or --build
# It never stops or removes anything: cleaning up is the caller's, for its own project only.
set -euo pipefail

compose() { docker compose ${COMPOSE_ENV_FILE:+--env-file "$COMPOSE_ENV_FILE"} "$@"; }
api="http://127.0.0.1:${API_PORT:-8000}"
step() { echo "smoke: $*"; }
fail() { echo "smoke: FAILED: $*" >&2; exit 1; }
# A field of the JSON document on stdin, e.g. json id.
json() { python3 -c "import json, sys; print(json.load(sys.stdin)[sys.argv[1]])" "$1"; }
# A query, as the admin, inside the db container (its credentials never leave it).
sql() {
  compose exec -T db sh -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -AtX -v ON_ERROR_STOP=1 -c "$1"' sh "$1"
}

step "starting db -> bootstrap -> migrate -> api"
# shellcheck disable=SC2086  # SMOKE_UP_ARGS is a list of arguments
compose up --detach --wait --wait-timeout 180 ${SMOKE_UP_ARGS:-}

for one_shot in bootstrap migrate; do
  id=$(compose ps --all --quiet "$one_shot")
  [ -n "$id" ] || fail "$one_shot never ran"
  code=$(docker inspect --format '{{.State.ExitCode}}' "$id")
  [ "$code" = 0 ] || fail "$one_shot exited with $code"
done
step "bootstrap and migrate completed"

curl --fail --silent --show-error "$api/health/live" >/dev/null
curl --fail --silent --show-error "$api/health/ready" >/dev/null || fail "the API is not ready"
# Every response carries the request's id (ADR 0017).
curl --fail --silent --show-error --include "$api/health/live" | grep -qi '^x-request-id: ' \
  || fail "no X-Request-ID header"
step "the API is live and ready, and answers with request ids"

# --- an API flow, as a client ------------------------------------------------------------------
token=$(python3 -c "import secrets; print(secrets.token_hex(4))")
email="smoke-$token@example.com"
password="correct horse battery staple"  # synthetic
credentials="{\"email\": \"$email\", \"password\": \"$password\"}"
user=$(curl -fsS -X POST "$api/users" -H 'Content-Type: application/json' -d "$credentials" | json id)
access=$(curl -fsS -X POST "$api/sessions" -H 'Content-Type: application/json' -d "$credentials" \
  | json access_token)
auth="Authorization: Bearer $access"
account=$(curl -fsS -X POST "$api/users/$user/accounts" -H "$auth" \
  -H 'Content-Type: application/json' -d '{"currency": "GBP"}' | json id)
balance=$(curl -fsS -X POST "$api/users/$user/accounts/$account/deposits" -H "$auth" \
  -H "Idempotency-Key: smoke-$token" -H 'Content-Type: application/json' \
  -d '{"amount_minor": 2500}' | json balance_after_minor)
[ "$balance" = 2500 ] || fail "deposit: balance $balance"
step "registered, logged in, opened an account and deposited"

# --- what the API container holds ---------------------------------------------------------------
names=$(compose exec -T api env | cut -d= -f1)
for forbidden in ADMIN_DATABASE_URL POSTGRES_PASSWORD POSTGRES_USER LEDGER_OWNER_PASSWORD \
  LEDGER_APP_PASSWORD; do
  if grep -qx "$forbidden" <<<"$names"; then fail "the api container has $forbidden"; fi
done
compose exec -T api sh -c \
  'case "$DATABASE_URL" in postgresql+asyncpg://ledger_app:*) ;; *) exit 1 ;; esac' \
  || fail "the api container's DATABASE_URL is not ledger_app's"
step "the api container holds only ledger_app's URL"

# --- the server's view ----------------------------------------------------------------------------
api_roles=$(sql "SELECT string_agg(DISTINCT usename, ',') FROM pg_stat_activity
                 WHERE application_name = 'banking-ledger-api'")
[ "$api_roles" = ledger_app ] || fail "the API is connected as: ${api_roles:-nothing}"
owners=$(sql "SELECT string_agg(DISTINCT pg_get_userbyid(owner), ',') FROM (
                SELECT relowner AS owner FROM pg_class WHERE relnamespace = 'public'::regnamespace
                UNION SELECT proowner FROM pg_proc WHERE pronamespace = 'public'::regnamespace) o")
[ "$owners" = ledger_owner ] || fail "public is owned by: $owners"
version=$(sql "SELECT version_num FROM alembic_version")
[ "$version" = 0014 ] || fail "schema version $version"
privileged=$(sql "SELECT count(*) FROM pg_roles
                  WHERE rolname IN ('ledger_owner', 'ledger_app', 'ledger_runtime')
                    AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls OR rolreplication)")
[ "$privileged" = 0 ] || fail "a ledger role is privileged"
step "the API runs as ledger_app; ledger_owner owns the schema (version $version)"

step "OK"
