#!/usr/bin/env bash
# Backup/restore rehearsal for the development database (Phase 10, before migrating it 0007 -> 0013).
#
#   1. back up the development database `ledger` (read-only: pg_dump, roles without passwords);
#   2. restore the dump into a NEW, uniquely named database on the disposable test cluster;
#   3. compare the restored copy with the source, read-only: revision, schema, ownership, ACLs,
#      triggers, sequences, data, integrity;
#   4. drop that one database again, after re-verifying exactly what it is.
#
# Usage:
#   scripts/rehearse-backup-restore.sh --backup-dir DIR                # plan only (the default)
#   scripts/rehearse-backup-restore.sh --backup-dir DIR --execute      # do it
#   scripts/rehearse-backup-restore.sh --backup-dir DIR --execute --keep-restored
#   scripts/rehearse-backup-restore.sh --execute --cleanup NAME --expected-oid OID \
#     --expected-cluster SYSTEM_IDENTIFIER
#
# Without --execute nothing runs: no docker command, no database contact. With it, every step is
# gated: the containers and servers are identified first (compose labels, image, storage, system
# identifier, the test cluster's server-side marker) and any failed check stops the script at
# once, without touching anything further. Writes happen only on the test cluster, only to the
# database this run creates (recorded by OID). Nothing is ever written to the development server.
# The dump files are kept: they are the real backup to take before migrating.
#
# Secrets: commands run inside the containers over the local socket as the container's own
# POSTGRES_USER (trusted locally), so no password or URL is read, passed or printed. The role dump
# uses --no-role-passwords. The optional bootstrap-and-upgrade rehearsal of the restored copy is
# deliberately not implemented here.
#
# Not covered: database-level properties of `ledger` (its ACL, e.g. CONNECT, and ALTER DATABASE
# settings). pg_dump without --create does not dump them, so they are neither restored nor
# compared; after a real restore the role bootstrap re-applies the database-level revokes.
set -euo pipefail
set -o noclobber # never overwrite a file through a redirection

DEV_CONTAINER=banking-ledger-db-1
DEV_SERVICE=db
DEV_DATABASE=ledger
TEST_CONTAINER=banking-ledger-test-db-1
TEST_SERVICE=test-db
COMPOSE_PROJECT=banking-ledger
POSTGRES_IMAGE=postgres:18-alpine
DISPOSABLE_SETTING=ledger.test_cluster
DISPOSABLE_VALUE=disposable
TARGET_PATTERN='^ledger_restore_rehearsal_[0-9]{14}_test$'

execute=false
keep_restored=false
backup_dir=""
cleanup_name=""
expected_oid=""
expected_cluster=""
dev_cluster="" # system identifiers, set by verify_containers
test_cluster=""
leftover="" # "name oid cluster" of the database this run created and has not yet dropped or kept

# Whatever ends the run (a refusal, a failure, an interruption): if this run's database is still
# there, say so and how to remove it, gated. Never removes anything by itself.
report_leftover() {
  local status=$?
  if [ -n "$leftover" ]; then
    set -- $leftover
    printf 'rehearsal: %s (OID %s) was left on %s; remove it with:\n  %s --execute --cleanup %s --expected-oid %s --expected-cluster %s\n' \
      "$1" "$2" "$TEST_CONTAINER" "$0" "$1" "$2" "$3" >&2
  fi
  exit "$status"
}
trap report_leftover EXIT

say() { printf 'rehearsal: %s\n' "$*"; }
# A failed safety check: stop at once, touch nothing more, no automatic recovery.
refuse() {
  printf 'rehearsal: REFUSED: %s\n' "$*" >&2
  exit 2
}
fail() {
  printf 'rehearsal: FAILED: %s\n' "$*" >&2
  exit 1
}
usage() { sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --backup-dir) backup_dir="${2:?--backup-dir needs a directory}"; shift 2 ;;
    --execute) execute=true; shift ;;
    --keep-restored) keep_restored=true; shift ;;
    --cleanup) cleanup_name="${2:?--cleanup needs a database name}"; shift 2 ;;
    --expected-oid) expected_oid="${2:?--expected-oid needs an OID}"; shift 2 ;;
    --expected-cluster) expected_cluster="${2:?--expected-cluster needs a system identifier}"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; refuse "unknown argument: $1" ;;
  esac
done

# --- read-only helpers (used only with --execute) ---------------------------------------------

# Run SQL inside a container, as its POSTGRES_USER over the local socket. read_only: the server
# refuses any write in that session (default_transaction_read_only=on). settings: more -c options.
psql_in() { # container database read_only sql [settings]
  local container=$1 database=$2 read_only=$3 sql=$4 options=${5:-}
  [ "$read_only" = read-only ] && options="-c default_transaction_read_only=on $options"
  docker exec -e PGOPTIONS="$options" "$container" sh -c \
    'psql -U "$POSTGRES_USER" -d "$1" -X -A -t -q -v ON_ERROR_STOP=1 -c "$2"' sh "$database" "$sql"
}
ro() { psql_in "$1" "$2" read-only "$3" "${4:-}"; }

inspect() { docker inspect --format "$2" "$1" 2>/dev/null || refuse "container $1 not found"; }

# Who each container is, from Docker's metadata and from the server itself.
verify_containers() {
  local container service
  for container in "$DEV_CONTAINER" "$TEST_CONTAINER"; do
    [ "$(inspect "$container" '{{.State.Running}}')" = true ] || refuse "$container is not running"
    [ "$(inspect "$container" '{{.Config.Image}}')" = "$POSTGRES_IMAGE" ] \
      || refuse "$container does not run $POSTGRES_IMAGE"
    [ "$(inspect "$container" '{{index .Config.Labels "com.docker.compose.project"}}')" \
      = "$COMPOSE_PROJECT" ] || refuse "$container is not in compose project $COMPOSE_PROJECT"
  done
  service=$(inspect "$DEV_CONTAINER" '{{index .Config.Labels "com.docker.compose.service"}}')
  [ "$service" = "$DEV_SERVICE" ] || refuse "$DEV_CONTAINER is not compose service $DEV_SERVICE"
  service=$(inspect "$TEST_CONTAINER" '{{index .Config.Labels "com.docker.compose.service"}}')
  [ "$service" = "$TEST_SERVICE" ] || refuse "$TEST_CONTAINER is not compose service $TEST_SERVICE"
  # The test cluster keeps its data on tmpfs and mounts no volume: nothing on it outlives it.
  [ "$(inspect "$TEST_CONTAINER" '{{json .HostConfig.Tmpfs}}')" = '{"/var/lib/postgresql":""}' ] \
    || refuse "$TEST_CONTAINER does not keep its data on tmpfs"
  [ "$(inspect "$TEST_CONTAINER" '{{len .Mounts}}')" = 0 ] || refuse "$TEST_CONTAINER mounts a volume"

  # The servers: the test one carries the disposable marker, the development one does not, and
  # they are different clusters (system identifiers, unique per initdb).
  local marker dev_marker
  marker=$(ro "$TEST_CONTAINER" postgres "SELECT current_setting('$DISPOSABLE_SETTING', true)")
  [ "$marker" = "$DISPOSABLE_VALUE" ] \
    || refuse "$TEST_CONTAINER lacks $DISPOSABLE_SETTING=$DISPOSABLE_VALUE (run make test once)"
  dev_marker=$(ro "$DEV_CONTAINER" postgres "SELECT current_setting('$DISPOSABLE_SETTING', true)")
  [ -z "$dev_marker" ] || refuse "$DEV_CONTAINER carries the disposable marker: wrong container?"
  dev_cluster=$(ro "$DEV_CONTAINER" postgres "SELECT system_identifier FROM pg_control_system()")
  test_cluster=$(ro "$TEST_CONTAINER" postgres "SELECT system_identifier FROM pg_control_system()")
  [[ "$dev_cluster" =~ ^[0-9]+$ && "$test_cluster" =~ ^[0-9]+$ ]] \
    || refuse "could not read the system identifiers"
  [ "$dev_cluster" != "$test_cluster" ] || refuse "development and test containers are the same cluster"
  say "verified: $DEV_CONTAINER (service $DEV_SERVICE, cluster $dev_cluster)"
  say "verified: $TEST_CONTAINER (service $TEST_SERVICE, tmpfs, marked, cluster $test_cluster)"
}

major_of_tool() { docker exec "$1" "$2" --version | sed -E 's/.* ([0-9]+)(\.[0-9]+)*.*/\1/'; }
major_of_server() { ro "$1" postgres "SELECT current_setting('server_version_num')::int / 10000"; }

verify_versions() {
  local dev_server dump_tool test_server restore_tool
  dev_server=$(major_of_server "$DEV_CONTAINER")
  dump_tool=$(major_of_tool "$DEV_CONTAINER" pg_dump)
  test_server=$(major_of_server "$TEST_CONTAINER")
  restore_tool=$(major_of_tool "$TEST_CONTAINER" pg_restore)
  [ "$dump_tool" = "$dev_server" ] || refuse "pg_dump $dump_tool for server $dev_server"
  [ "$restore_tool" = "$test_server" ] || refuse "pg_restore $restore_tool for server $test_server"
  [ "$test_server" -ge "$dev_server" ] || refuse "cannot restore $dev_server into $test_server"
  say "verified: pg_dump $dump_tool = server $dev_server; pg_restore $restore_tool = server $test_server"
}

database_oid() { # container name: its OID, or nothing
  ro "$1" postgres "SELECT oid FROM pg_database WHERE datname = '$2'"
}

# --- the backup directory (local checks only) ------------------------------------------------------

verify_backup_dir() {
  [ -n "$backup_dir" ] || refuse "--backup-dir is required"
  [ -d "$backup_dir" ] || refuse "backup directory does not exist: $backup_dir (create it, mode 700)"
  [ -O "$backup_dir" ] || refuse "backup directory is not owned by you"
  local mode real repo
  mode=$(stat -c '%a' "$backup_dir")
  [ "$mode" = 700 ] || refuse "backup directory mode is $mode; it must be 700"
  real=$(realpath "$backup_dir")
  repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
  case "$real/" in "$repo"/*) refuse "backup directory is inside the repository ($repo)" ;; esac
  backup_dir=$real
}

# --- cleanup: drop exactly the database this rehearsal created ------------------------------------

# An OID is unique only within one cluster: the database is identified by both.
drop_rehearsal_database() { # name oid cluster
  local name=$1 oid=$2 cluster=$3 current
  [[ "$name" =~ $TARGET_PATTERN ]] || refuse "refusing to drop $name: not a rehearsal database name"
  [[ "$oid" =~ ^[0-9]+$ ]] || refuse "refusing to drop $name: no recorded OID"
  [[ "$cluster" =~ ^[0-9]+$ ]] || refuse "refusing to drop $name: no recorded cluster"
  verify_containers # again, right before the drop
  [ "$test_cluster" = "$cluster" ] \
    || refuse "refusing to drop $name: $TEST_CONTAINER is cluster $test_cluster, not the recorded $cluster"
  current=$(database_oid "$TEST_CONTAINER" "$name")
  [ "$current" = "$oid" ] \
    || refuse "refusing to drop $name: its OID is '${current:-none}', not the recorded $oid"
  # Plain DROP: no IF EXISTS, no FORCE. A session still connected makes it fail, and stop.
  psql_in "$TEST_CONTAINER" postgres read-write "DROP DATABASE \"$name\""
  [ -z "$(database_oid "$TEST_CONTAINER" "$name")" ] || fail "$name still exists after DROP"
  say "dropped $name (OID $oid) on $TEST_CONTAINER"
}

if [ -n "$cleanup_name" ]; then
  # Checked here too, so the plan never promises a drop that --execute would refuse.
  [[ "$cleanup_name" =~ $TARGET_PATTERN ]] \
    || refuse "refusing to drop $cleanup_name: not a rehearsal database name"
  [[ "$expected_oid" =~ ^[0-9]+$ ]] || refuse "refusing to drop $cleanup_name: --expected-oid is required"
  [[ "$expected_cluster" =~ ^[0-9]+$ ]] \
    || refuse "refusing to drop $cleanup_name: --expected-cluster is required"
  $execute || { say "plan: would verify the containers, then drop $cleanup_name only if its OID is $expected_oid on cluster $expected_cluster (add --execute)"; exit 0; }
  drop_rehearsal_database "$cleanup_name" "$expected_oid" "$expected_cluster"
  exit 0
fi

# --- the plan --------------------------------------------------------------------------------------

verify_backup_dir
stamp=$(date -u +%Y%m%d%H%M%S)
target="ledger_restore_rehearsal_${stamp}_test"
[[ "$target" =~ $TARGET_PATTERN ]] || refuse "generated target name $target is invalid"
dump_file="$backup_dir/ledger-$stamp.dump"
roles_file="$backup_dir/roles-$stamp.sql"
sums_file="$backup_dir/SHA256SUMS-$stamp"
for file in "$dump_file" "$dump_file.partial" "$roles_file" "$roles_file.partial" "$sums_file"; do
  [ ! -e "$file" ] || refuse "$file already exists"
done

cat <<PLAN
rehearsal: plan ($( $execute && echo EXECUTING || echo "plan only: nothing runs without --execute"))
  verify  $DEV_CONTAINER: running, $POSTGRES_IMAGE, compose $COMPOSE_PROJECT/$DEV_SERVICE, no disposable marker
  verify  $TEST_CONTAINER: running, $POSTGRES_IMAGE, compose $COMPOSE_PROJECT/$TEST_SERVICE, tmpfs, no mounts,
          $DISPOSABLE_SETTING=$DISPOSABLE_VALUE; system identifier different from the development one
  verify  pg_dump/pg_restore majors match their servers
  backup  $DEV_CONTAINER/$DEV_DATABASE (read-only) -> $dump_file
          roles without passwords -> $roles_file; SHA-256 -> $sums_file
  restore into NEW database $target on $TEST_CONTAINER (must not exist; OID recorded),
          pg_restore --exit-on-error --single-transaction
  compare source and copy, read-only: revision, columns, constraints, indexes, triggers, functions,
          ownership and ACLs, sequences, row counts and contents; integrity checks on the copy
  cleanup $( $keep_restored && echo "keep $target (--keep-restored)" || echo "drop $target after re-verifying containers, marker, name and OID" )
  keep    the backup files (they are the backup)
PLAN

$execute || exit 0

# --- execute ---------------------------------------------------------------------------------------

verify_containers
verify_versions
[ -n "$(database_oid "$DEV_CONTAINER" "$DEV_DATABASE")" ] || refuse "$DEV_DATABASE not found"

say "backing up $DEV_DATABASE (read-only)"
docker exec -e PGOPTIONS="-c default_transaction_read_only=on" "$DEV_CONTAINER" sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$1" --format=custom' sh "$DEV_DATABASE" >"$dump_file.partial" \
  || fail "pg_dump failed; $dump_file.partial is incomplete and not a backup"
docker exec -e PGOPTIONS="-c default_transaction_read_only=on" "$DEV_CONTAINER" sh -c \
  'pg_dumpall -U "$POSTGRES_USER" --roles-only --no-role-passwords' >"$roles_file.partial" \
  || fail "pg_dumpall failed; $roles_file.partial is incomplete and not a backup"
# Only a complete dump gets the final name.
mv -n -- "$dump_file.partial" "$dump_file"
mv -n -- "$roles_file.partial" "$roles_file"
chmod 600 "$dump_file" "$roles_file"
(cd "$backup_dir" && sha256sum "$(basename "$dump_file")" "$(basename "$roles_file")") >"$sums_file"
chmod 600 "$sums_file"
(cd "$backup_dir" && sha256sum --check --quiet "$(basename "$sums_file")") || fail "checksum mismatch"
listing=$(docker exec -i "$TEST_CONTAINER" pg_restore --list <"$dump_file")
grep -q 'TABLE DATA public alembic_version' <<<"$listing" || fail "the dump has no alembic_version data"
say "backup written and checked: $(basename "$dump_file") ($(grep -c ' TABLE public ' <<<"$listing") tables)"

[ -z "$(database_oid "$TEST_CONTAINER" "$target")" ] || refuse "$target already exists"
psql_in "$TEST_CONTAINER" postgres read-write "CREATE DATABASE \"$target\""
oid=$(database_oid "$TEST_CONTAINER" "$target")
[[ "$oid" =~ ^[0-9]+$ ]] || fail "created $target but could not read its OID: drop it by hand"
leftover="$target $oid $test_cluster"
say "created $target (OID $oid) on $TEST_CONTAINER"

docker exec -i "$TEST_CONTAINER" sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$1" --exit-on-error --single-transaction' sh "$target" \
  <"$dump_file"
say "restored into $target"

# --- compare, read-only ----------------------------------------------------------------------------
# Byte-stable on both servers: lines sorted and rows ordered by "C" collation, and the settings
# that change how a row prints (timestamptz above all) pinned for the session.
FINGERPRINT_SETTINGS="-c TimeZone=UTC -c DateStyle=ISO -c IntervalStyle=postgres -c extra_float_digits=1 -c bytea_output=hex"
read -r -d '' FINGERPRINTS <<'SQL' || true
SELECT line FROM (
SELECT 'revision ' || version_num FROM alembic_version
UNION ALL
SELECT format('column %s.%s %s %s notnull=%s default=%s identity=%s', c.relname, a.attnum,
              a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
              coalesce(pg_get_expr(d.adbin, d.adrelid), '-'), a.attidentity)
  FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
  LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
 WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
UNION ALL
SELECT format('constraint %s %s %s deferrable=%s deferred=%s validated=%s', conrelid::regclass,
              conname, pg_get_constraintdef(oid), condeferrable, condeferred, convalidated)
  FROM pg_constraint WHERE connamespace = 'public'::regnamespace
UNION ALL
SELECT 'index ' || pg_get_indexdef(indexrelid) FROM pg_index
 WHERE indrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'public'::regnamespace)
UNION ALL
SELECT format('trigger %s enabled=%s', pg_get_triggerdef(oid), tgenabled) FROM pg_trigger
 WHERE NOT tgisinternal
   AND tgrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'public'::regnamespace)
UNION ALL
SELECT format('function %s(%s) owner=%s definer=%s config=%s acl=%s body=%s', proname,
              pg_get_function_identity_arguments(oid), pg_get_userbyid(proowner), prosecdef,
              proconfig, proacl, md5(pg_get_functiondef(oid)))
  FROM pg_proc WHERE pronamespace = 'public'::regnamespace
UNION ALL
SELECT format('relation %s kind=%s owner=%s acl=%s', relname, relkind, pg_get_userbyid(relowner),
              relacl)
  FROM pg_class WHERE relnamespace = 'public'::regnamespace
UNION ALL
SELECT format('column-acl %s.%s %s', c.relname, a.attname, a.attacl)
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
 WHERE c.relnamespace = 'public'::regnamespace AND a.attacl IS NOT NULL
UNION ALL
SELECT format('schema public owner=%s acl=%s', pg_get_userbyid(nspowner), nspacl)
  FROM pg_namespace WHERE nspname = 'public'
UNION ALL
SELECT format('default-acl %s %s %s', pg_get_userbyid(defaclrole), defaclobjtype, defaclacl)
  FROM pg_default_acl
UNION ALL
SELECT format('sequence %s last_value=%s', sequencename, last_value) FROM pg_sequences
 WHERE schemaname = 'public'
UNION ALL
SELECT format('data %s rows=%s md5=%s', relname,
              (xpath('/row/n/text()', query_to_xml(format(
                  'SELECT count(*) AS n FROM public.%I', relname), false, true, '')))[1],
              (xpath('/row/h/text()', query_to_xml(format(
                  'SELECT md5(coalesce(string_agg(t::text, ''|'' ORDER BY t::text COLLATE "C"), '''')) '
                  'AS h FROM public.%I t', relname), false, true, '')))[1])
  FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'
) fingerprint(line)
ORDER BY line COLLATE "C"
SQL
source_fingerprint=$(ro "$DEV_CONTAINER" "$DEV_DATABASE" "$FINGERPRINTS" "$FINGERPRINT_SETTINGS")
copy_fingerprint=$(ro "$TEST_CONTAINER" "$target" "$FINGERPRINTS" "$FINGERPRINT_SETTINGS")
[ -n "$source_fingerprint" ] || fail "empty fingerprint of the source"
if [ "$source_fingerprint" != "$copy_fingerprint" ]; then
  diff <(printf '%s\n' "$source_fingerprint") <(printf '%s\n' "$copy_fingerprint") >&2 || true
  fail "the restored copy differs from the source (see above)"
fi
say "copy matches the source: $(wc -l <<<"$source_fingerprint") fingerprint lines, $(grep '^revision' <<<"$source_fingerprint")"

read -r -d '' INTEGRITY <<'SQL' || true
SELECT
 (SELECT count(*) FROM accounts a WHERE a.balance_minor <> coalesce(
    (SELECT sum(e.amount_minor) FROM ledger_entries e WHERE e.account_id = a.id), 0))
 + (SELECT count(*) FROM (SELECT ledger_id FROM ledger_entries GROUP BY ledger_id
                          HAVING sum(amount_minor) <> 0) x)
 + (SELECT count(*) FROM (SELECT ledger_id FROM accounts GROUP BY ledger_id
                          HAVING sum(balance_minor) <> 0) x)
 + (SELECT count(*) FROM transactions t
     WHERE (SELECT count(*) FROM ledger_entries e WHERE e.transaction_id = t.id) <> t.entry_count
        OR (SELECT coalesce(sum(amount_minor), 0) FROM ledger_entries e
             WHERE e.transaction_id = t.id) <> 0)
 + (SELECT count(*) FROM accounts WHERE kind = 'customer' AND balance_minor < 0)
SQL
violations=$(ro "$TEST_CONTAINER" "$target" "$INTEGRITY")
[ "$violations" = 0 ] || fail "integrity checks on the copy found $violations violation(s)"
say "integrity of the copy: balances, ledgers and transactions consistent"

if $keep_restored; then
  say "kept $target (OID $oid). Remove it with: $0 --execute --cleanup $target --expected-oid $oid --expected-cluster $test_cluster"
else
  drop_rehearsal_database "$target" "$oid" "$test_cluster"
fi
leftover=""
say "OK. Backup kept: $dump_file, $roles_file, $sums_file"
