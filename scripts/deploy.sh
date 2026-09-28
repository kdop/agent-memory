#!/usr/bin/env bash
# Put a tagged release live: back up the database, check the tag out in the
# release checkout, install, migrate, restart the service, check that it answers.
#
#   scripts/deploy.sh deploy <tag>     back up, check out the tag, install, migrate up, restart
#   scripts/deploy.sh rollback <tag>   back up, migrate down to the tag's schema, check out, install, restart
#   scripts/deploy.sh status           tag or commit of the release checkout, unit state, health
#
# Options, before or after the command:
#   --release <path>   the release checkout (default $HOME/workspace/agent-memory-live)
#   --unit <name>      the systemd user unit to restart (default agent-memory)
#   --dsn <dsn>        the database, instead of AGENT_MEMORY_DB in the release checkout's .env
#   --no-restart       stop after the migration: no restart, no health check, no log
#
# The live service runs from the release checkout, which is always on a tag. A
# working checkout is never what runs. This script never runs against the folder
# it lives in, and stops when the release checkout has changed or untracked files.
#
# Each deploy or rollback writes a pg_dump of the database to
# <release>/backups/<date>-<tag>.sql and prints the row counts of memories, tags,
# memory_tags and memory_reviews before and after the migration. The first three
# must match; the run stops when they do not. Every step says what it did, and a
# failure names the step it stopped at.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE="$HOME/workspace/agent-memory-live"
UNIT=agent-memory
DSN=""
RESTART=true
TABLES="memories tags memory_tags memory_reviews"
DATA_TABLES="memories tags memory_tags"
HEALTH_WAIT=300        # seconds to wait for /health after a restart; the model may load first
STEP=""

log()  { STEP="$1"; printf '\n\033[1;36m▶ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[1;31m✗ %s%s\033[0m\n' "${STEP:+$STEP: }" "$1" >&2; exit 1; }
trap 'printf "  \033[1;31m✗ %sa command failed\033[0m\n" "${STEP:+$STEP: }" >&2' ERR

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

# ---- arguments --------------------------------------------------------------
CMD=""
TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --release)    [ -n "${2:-}" ] || usage; RELEASE="$2"; shift 2 ;;
    --release=*)  RELEASE="${1#*=}"; shift ;;
    --unit)       [ -n "${2:-}" ] || usage; UNIT="$2"; shift 2 ;;
    --unit=*)     UNIT="${1#*=}"; shift ;;
    --dsn)        [ -n "${2:-}" ] || usage; DSN="$2"; shift 2 ;;
    --dsn=*)      DSN="${1#*=}"; shift ;;
    --no-restart) RESTART=false; shift ;;
    -h|--help)    usage ;;
    -*)           echo "unknown option: $1" >&2; usage ;;
    *)
      if [ -z "$CMD" ]; then CMD="$1"
      elif [ -z "$TAG" ]; then TAG="$1"
      else usage; fi
      shift ;;
  esac
done

case "$CMD" in
  deploy|rollback) [ -n "$TAG" ] || usage ;;
  status)          [ -z "$TAG" ] || usage ;;
  *)               usage ;;
esac

# ---- helpers ----------------------------------------------------------------
VENV=""

# env_value <key> <default>: a value from the release checkout's .env.
env_value() {
  local v=""
  if [ -f "$RELEASE/.env" ]; then
    v=$(grep -E "^[[:space:]]*$1[[:space:]]*=" "$RELEASE/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'" | xargs || true)
  fi
  echo "${v:-$2}"
}

# The release checkout must exist, be a git checkout, and not be this checkout.
check_release() {
  log "release checkout"
  [ -d "$RELEASE" ] || fail "no release checkout at $RELEASE"
  RELEASE="$(cd "$RELEASE" && pwd)"
  VENV="$RELEASE/.venv"
  local top
  top=$(git -C "$RELEASE" rev-parse --show-toplevel 2>/dev/null) || fail "$RELEASE is not a git checkout"
  [ "$top" != "$HERE" ] || fail "$RELEASE is the folder this script lives in; run it from a working checkout against the release checkout"
  ok "$top"
}

check_clean() {
  local changes
  changes=$(git -C "$RELEASE" status --porcelain)
  if [ -n "$changes" ]; then
    printf '%s\n' "$changes" | sed 's/^/    /' >&2
    fail "the release checkout has changed or untracked files; it must be clean"
  fi
  ok "working tree clean"
}

describe() {
  git -C "$RELEASE" describe --tags --exact-match HEAD 2>/dev/null \
    || echo "commit $(git -C "$RELEASE" rev-parse --short HEAD) (not on a tag)"
}

resolve_dsn() {
  log "database"
  [ -n "$DSN" ] || DSN=$(env_value AGENT_MEMORY_DB "")
  [ -n "$DSN" ] || fail "no AGENT_MEMORY_DB in $RELEASE/.env and no --dsn"
  DSN="${DSN/postgresql+asyncpg:/postgresql:}"
  ok "$(echo "$DSN" | sed -E 's#(://[^:/]+):[^@]*@#\1:***@#')"
}

sql() { psql -X -A -t -q -v ON_ERROR_STOP=1 "$DSN" -c "$1"; }

# count <table>: the row count, or - when the table does not exist yet.
count() {
  local exists
  exists=$(sql "SELECT to_regclass('public.$1') IS NOT NULL")
  if [ "$exists" = "t" ]; then sql "SELECT count(*) FROM $1"; else echo "-"; fi
}

# counts: one line per table, "table count", for the four tables.
counts() {
  local t
  for t in $TABLES; do printf '%s %s\n' "$t" "$(count "$t")"; done
}

print_counts() { printf '%s\n' "$1" | awk '{ printf "  %-16s %10s\n", $1, $2 }'; }

fetch_tags() {
  log "fetching tags"
  git -C "$RELEASE" fetch --tags --quiet || fail "git fetch --tags failed in $RELEASE"
  git -C "$RELEASE" rev-parse -q --verify "refs/tags/$TAG^{commit}" >/dev/null \
    || fail "tag $TAG not found in $RELEASE"
  ok "tag $TAG is $(git -C "$RELEASE" rev-parse --short "$TAG^{commit}")"
}

backup() {
  local dir="$RELEASE/backups" file
  file="$dir/$(date +%Y-%m-%d-%H%M%S)-$1.sql"
  log "backup"
  mkdir -p "$dir"
  pg_dump --no-owner --no-privileges -f "$file" "$DSN" || fail "pg_dump failed"
  ok "$file ($(du -h "$file" | cut -f1))"
  BEFORE=$(counts)
  print_counts "$BEFORE"
}

checkout_tag() {
  log "checking out $TAG"
  git -C "$RELEASE" checkout --quiet "$TAG" || fail "git checkout $TAG failed"
  ok "$(describe)"
}

install() {
  log "installing"
  if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV" || fail "could not create $VENV"
    ok "created $VENV"
  fi
  (cd "$RELEASE" && "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -e '.[server,mcp,embed]') \
    || fail "pip install failed"
  ok "agent-memory $("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("agent-memory"))') in $VENV"
}

alembic() { (cd "$RELEASE" && AGENT_MEMORY_DB="$DSN" "$VENV/bin/alembic" "$@"); }

migrate_up() {
  log "migrating up"
  alembic upgrade head || fail "alembic upgrade head failed"
  ok "schema at $(alembic current 2>/dev/null | tr -d '\n')"
}

# tag_revision: the alembic head of the tag, read from the tag's own migration
# files with git show: the revision that no other revision builds on. Reading
# the files this way needs no second checkout.
tag_revision() {
  local files f revs="" downs="" r d heads=""
  files=$(git -C "$RELEASE" ls-tree -r --name-only "$TAG" -- alembic/versions | grep '\.py$' || true)
  [ -n "$files" ] || fail "tag $TAG has no alembic/versions"
  for f in $files; do
    r=$(git -C "$RELEASE" show "$TAG:$f" | sed -n -E "s/^revision[^=]*= *['\"]([0-9a-f]+)['\"].*/\1/p" | head -1)
    d=$(git -C "$RELEASE" show "$TAG:$f" | sed -n -E "s/^down_revision[^=]*= *['\"]([0-9a-f]+)['\"].*/\1/p" | head -1)
    [ -n "$r" ] || continue
    revs="$revs $r"
    downs="$downs ${d:-none}"
  done
  for r in $revs; do
    case " $downs " in *" $r "*) ;; *) heads="$heads $r" ;; esac
  done
  set -- $heads
  [ $# -eq 1 ] || fail "tag $TAG has $# alembic heads, expected one:$heads"
  echo "$1"
}

migrate_down() {
  local rev
  log "migrating down to the schema of $TAG"
  rev=$(tag_revision)
  ok "alembic head of $TAG is $rev"
  alembic downgrade "$rev" || fail "alembic downgrade $rev failed"
  ok "schema at $(alembic current 2>/dev/null | tr -d '\n')"
}

compare_counts() {
  log "row counts after"
  AFTER=$(counts)
  print_counts "$AFTER"
  local t b a same=true
  for t in $DATA_TABLES; do
    b=$(printf '%s\n' "$BEFORE" | awk -v t="$t" '$1 == t { print $2 }')
    a=$(printf '%s\n' "$AFTER" | awk -v t="$t" '$1 == t { print $2 }')
    # A table that did not exist before (-) had no rows: the first deploy creates it.
    [ "${b/-/0}" = "${a/-/0}" ] || { same=false; echo "  $t: $b before, $a after" >&2; }
  done
  $same || fail "row counts changed"
  ok "memories, tags and memory_tags unchanged"
}

restart_unit() {
  local host port url start deadline state
  host=$(env_value AGENT_MEMORY_HOST 127.0.0.1)
  port=$(env_value AGENT_MEMORY_PORT 8099)
  url="http://$host:$port/health"
  log "restarting $UNIT"
  start=$(date '+%Y-%m-%d %H:%M:%S')
  systemctl --user restart "$UNIT" || fail "systemctl --user restart $UNIT failed"
  ok "restarted at $start"

  log "waiting for $url"
  deadline=$((SECONDS + HEALTH_WAIT))
  until curl -sf "$url" >/dev/null 2>&1; do
    state=$(systemctl --user is-active "$UNIT" 2>/dev/null || true)
    if [ "$state" = "failed" ] || [ "$state" = "inactive" ]; then
      journalctl --user -u "$UNIT" --since "$start" --no-pager -o cat | tail -20 >&2
      fail "$UNIT is $state"
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      journalctl --user -u "$UNIT" --since "$start" --no-pager -o cat | tail -20 >&2
      fail "no answer from $url after ${HEALTH_WAIT}s"
    fi
    sleep 1
  done
  ok "$(curl -s "$url")"

  log "log lines since the restart"
  journalctl --user -u "$UNIT" --since "$start" --no-pager -o cat \
    | grep -E 'reindex|poll|review model|review is|embedding' | sed 's/^/  /' || echo "  (no reindex or poll lines yet)"
}

# ---- commands ---------------------------------------------------------------
cmd_deploy() {
  check_release
  check_clean
  resolve_dsn
  fetch_tags
  backup "$TAG"
  checkout_tag
  install
  migrate_up
  compare_counts
  if $RESTART; then restart_unit; fi
  log "done"
  ok "$RELEASE is at $(describe)"
}

cmd_rollback() {
  check_release
  check_clean
  resolve_dsn
  fetch_tags
  backup "rollback-$TAG"
  migrate_down
  checkout_tag
  install
  migrate_up
  compare_counts
  if $RESTART; then restart_unit; fi
  log "done"
  ok "$RELEASE is back at $(describe)"
}

cmd_status() {
  local host port state changes
  check_release
  ok "$(describe)"
  changes=$(git -C "$RELEASE" status --porcelain)
  [ -z "$changes" ] && ok "working tree clean" || echo "  working tree has changes"
  log "unit $UNIT"
  state=$(systemctl --user is-active "$UNIT" 2>/dev/null || true)
  echo "  ${state:-unknown}"
  host=$(env_value AGENT_MEMORY_HOST 127.0.0.1)
  port=$(env_value AGENT_MEMORY_PORT 8099)
  log "health at http://$host:$port/health"
  echo "  $(curl -sf --max-time 3 "http://$host:$port/health" 2>/dev/null || echo "not answering")"
}

"cmd_$CMD"
