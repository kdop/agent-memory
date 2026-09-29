#!/usr/bin/env bash
# Work on a copy of the live database without touching it.
#
#   scripts/db_copy.sh up                    start a Postgres 16 container named memcopy
#   scripts/db_copy.sh load <source-dsn>     dump the source into the copy, then compare row counts
#   scripts/db_copy.sh verify <source-dsn>   compare row counts of source and copy
#   scripts/db_copy.sh down                  remove the container and its volume
#
# The copy listens on 127.0.0.1:5434 and keeps its data in the podman volume
# memcopy-data, so it survives a container restart. `up` and `load` print the copy's
# DSN when they finish.
#
# The source is only read. The script runs pg_dump and three SELECT count(*) against
# it and nothing else. `load` and `verify` exit 1 when the counts of memories, tags
# and memory_tags differ between source and copy.
#
# Needs podman only. pg_dump and psql run inside the memcopy container, so their
# version always matches the copy (a newer pg_dump on the host emits settings an
# older server rejects). The container shares the host's network, so a source DSN
# that works on the host works inside it too.

set -euo pipefail

CONTAINER=memcopy
VOLUME=memcopy-data
PORT=5434
IMAGE=docker.io/library/postgres:16
DB_USER=memory
DB_PASS=memory
DB_NAME=memory
COPY_DSN="postgresql://$DB_USER:$DB_PASS@127.0.0.1:$PORT/$DB_NAME"
TABLES="memories tags memory_tags"

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[1;31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

usage() {
  sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

# pg <tool> <args>: run a Postgres client tool inside the container.
# PGPORT is set on the container to move the copy to 5434; drop it here so a
# source DSN without a port dials the source's default port, not 5434.
pg() { podman exec -i "$CONTAINER" env -u PGPORT "$@"; }

running() { [ "$(podman inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; }

need_running() { running || fail "container $CONTAINER is not running; run '$0 up' first"; }

# Check over TCP: on first start the image runs a short-lived server that only
# listens on a socket while it sets the data directory up. That one must not count.
wait_ready() {
  local deadline=$((SECONDS + 30))
  until podman exec "$CONTAINER" pg_isready -q -h 127.0.0.1 -p "$PORT" -U "$DB_USER" -d "$DB_NAME" 2>/dev/null; do
    [ "$SECONDS" -lt "$deadline" ] || fail "$CONTAINER did not accept connections in time"
    sleep 0.5
  done
}

# count <dsn> <table>: one SELECT count(*), nothing else.
count() { pg psql -X -A -t -q -v ON_ERROR_STOP=1 "$1" -c "SELECT count(*) FROM $2"; }

# compare <source-dsn>: print the counts side by side, exit 1 if any differ.
compare() {
  local src="$1" table s c same=true
  log "row counts"
  printf '  %-12s %10s %10s\n' table source copy
  for table in $TABLES; do
    s=$(count "$src" "$table")
    c=$(count "$COPY_DSN" "$table")
    printf '  %-12s %10s %10s\n' "$table" "$s" "$c"
    [ "$s" = "$c" ] || same=false
  done
  $same && ok "counts match" || fail "counts differ"
}

# ---- commands ---------------------------------------------------------------
cmd_up() {
  log "starting $CONTAINER on port $PORT"
  if podman container exists "$CONTAINER"; then
    running || podman start "$CONTAINER" >/dev/null
  else
    podman volume exists "$VOLUME" || podman volume create "$VOLUME" >/dev/null
    # Host network: the source is reachable by the same address as from the host.
    # PGPORT moves the server to 5434 and listen_addresses keeps it on loopback.
    podman run -d --name "$CONTAINER" --network=host \
      -e POSTGRES_USER="$DB_USER" -e POSTGRES_PASSWORD="$DB_PASS" -e POSTGRES_DB="$DB_NAME" \
      -e PGPORT="$PORT" -v "$VOLUME:/var/lib/postgresql/data" \
      "$IMAGE" -c listen_addresses=127.0.0.1 >/dev/null
  fi
  wait_ready
  ok "$CONTAINER is up"
  echo "$COPY_DSN"
}

cmd_load() {
  local src="${1:-}"
  [ -n "$src" ] || usage
  need_running

  # Start from an empty database so a second load does not pile onto the first.
  log "resetting the copy"
  podman exec "$CONTAINER" dropdb -U "$DB_USER" --if-exists --force "$DB_NAME"
  podman exec "$CONTAINER" createdb -U "$DB_USER" "$DB_NAME"
  ok "empty database $DB_NAME"

  log "dumping the source into the copy"
  pg pg_dump --no-owner --no-privileges "$src" \
    | pg psql -X -q -v ON_ERROR_STOP=1 "$COPY_DSN" >/dev/null
  ok "restore finished"

  compare "$src"
  echo "$COPY_DSN"
}

cmd_verify() {
  local src="${1:-}"
  [ -n "$src" ] || usage
  need_running
  compare "$src"
}

cmd_down() {
  log "removing $CONTAINER and $VOLUME"
  podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
  podman volume rm -f "$VOLUME" >/dev/null 2>&1 || true
  ok "removed"
}

case "${1:-}" in
  up)     cmd_up ;;
  load)   cmd_load "${2:-}" ;;
  verify) cmd_verify "${2:-}" ;;
  down)   cmd_down ;;
  *)      usage ;;
esac
