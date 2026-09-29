#!/usr/bin/env bash
# Run a second copy of the service on a copy of the database, to try new code
# without touching the live service.
#
#   scripts/test_instance.sh up <source-dsn>   copy the database, migrate the copy, start a server on 8001
#   scripts/test_instance.sh restart           stop and start the server, keep the copy
#   scripts/test_instance.sh status            is the server up, what does /health say
#   scripts/test_instance.sh logs              tail the server log
#   scripts/test_instance.sh down              stop the server, remove the copy
#
# The server runs from this checkout with the repo's .venv, on 127.0.0.1:8001,
# with the API token from AGENT_MEMORY_API_TOKEN (or the repo's .env). Search by
# meaning is on; the review is on in warn mode when AGENT_MEMORY_REVIEW_URL is
# set, off otherwise. Nothing here reads AGENT_MEMORY_DB: the only database it
# touches is the copy that scripts/db_copy.sh makes.
#
# Point the command at it with:  export AGENT_MEMORY_API=http://127.0.0.1:8001
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${AGENT_MEMORY_VENV:-$ROOT/.venv}"
[ -x "$VENV/bin/python" ] || VENV="$(cd "$ROOT/../../.." 2>/dev/null && pwd)/.venv"   # a worktree under .claude/worktrees
PORT="${TEST_INSTANCE_PORT:-8001}"
RUN="$ROOT/.test-instance"
PIDFILE="$RUN/server.pid"
LOG="$RUN/server.log"
COPY_DSN="postgresql://memory:memory@127.0.0.1:5434/memory"

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[1;31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

token() {
  if [ -n "${AGENT_MEMORY_API_TOKEN:-}" ]; then echo "$AGENT_MEMORY_API_TOKEN"; return; fi
  local f
  for f in "$ROOT/.env" "$ROOT/../../../.env"; do
    [ -f "$f" ] || continue
    grep -oE '^AGENT_MEMORY_API_TOKEN=.*' "$f" | head -1 | cut -d= -f2- | tr -d '"'"'" && return
  done
  true
}

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

cmd_up() {
  local src="${1:-}"
  [ -n "$src" ] || { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
  running && fail "already running (pid $(cat "$PIDFILE")); run '$0 down' first"
  [ -x "$VENV/bin/python" ] || fail "no virtualenv at $VENV; set AGENT_MEMORY_VENV"
  mkdir -p "$RUN"

  log "copy of the database"
  "$ROOT/scripts/db_copy.sh" up >/dev/null
  "$ROOT/scripts/db_copy.sh" load "$src"

  log "migrating the copy"
  (cd "$ROOT" && AGENT_MEMORY_DB="$COPY_DSN" PYTHONPATH="$ROOT/src" "$VENV/bin/alembic" upgrade head 2>&1 | tail -1)
  ok "schema at head"

  start_server
  echo
  echo "export AGENT_MEMORY_API=http://127.0.0.1:$PORT"
}

start_server() {
  log "starting the server on 127.0.0.1:$PORT"
  local tok; tok="$(token)"
  [ -n "$tok" ] || fail "no API token: set AGENT_MEMORY_API_TOKEN"
  local review="${AGENT_MEMORY_REVIEW:-off}"
  [ -n "${AGENT_MEMORY_REVIEW_URL:-}" ] && review="${AGENT_MEMORY_REVIEW:-warn}"
  # The dashboard: this checkout's build, else the main checkout's.
  local static="${AGENT_MEMORY_STATIC_DIR:-}"
  [ -n "$static" ] || for d in "$ROOT/web/dist" "$ROOT/../../../web/dist"; do [ -d "$d" ] && { static="$d"; break; }; done
  (cd "$ROOT" && env -i HOME="$HOME" PATH="$PATH" \
      AGENT_MEMORY_DB="$COPY_DSN" AGENT_MEMORY_API_TOKEN="$tok" \
      AGENT_MEMORY_HOST=127.0.0.1 AGENT_MEMORY_PORT="$PORT" \
      AGENT_MEMORY_STATIC_DIR="$static" \
      AGENT_MEMORY_EMBED_CACHE="$ROOT/.cache/fastembed" \
      AGENT_MEMORY_REVIEW="$review" \
      AGENT_MEMORY_REVIEW_URL="${AGENT_MEMORY_REVIEW_URL:-}" \
      AGENT_MEMORY_REVIEW_MODEL="${AGENT_MEMORY_REVIEW_MODEL:-qwen3:14b}" \
      AGENT_MEMORY_REVIEW_POLL="${AGENT_MEMORY_REVIEW_POLL:-60}" \
      PYTHONPATH="$ROOT/src" \
      setsid nohup "$VENV/bin/python" -m agent_memory.server >"$LOG" 2>&1 </dev/null &
   echo $! >"$PIDFILE")
  local i
  for i in $(seq 1 90); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null || { tail -20 "$LOG" >&2; fail "server did not come up"; }
  ok "up (pid $(cat "$PIDFILE"), review=$review, dashboard=${static:-none}, log $LOG)"
}

cmd_restart() {
  running && { kill "$(cat "$PIDFILE")"; sleep 1; ok "server stopped"; }
  rm -f "$PIDFILE"
  start_server
  echo
  echo "export AGENT_MEMORY_API=http://127.0.0.1:$PORT"
}

cmd_status() {
  if running; then ok "running (pid $(cat "$PIDFILE"))"; else echo "  not running"; fi
  curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null && echo
}

cmd_logs() { tail -n "${1:-50}" "$LOG"; }

cmd_down() {
  if running; then kill "$(cat "$PIDFILE")" && ok "server stopped"; fi
  rm -f "$PIDFILE"
  "$ROOT/scripts/db_copy.sh" down >/dev/null && ok "copy removed"
}

case "${1:-}" in
  up)     cmd_up "${2:-}" ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  logs)   cmd_logs "${2:-}" ;;
  down)   cmd_down ;;
  *)      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
