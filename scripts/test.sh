#!/usr/bin/env bash
# The single source of truth for "does this project work" — run it manually
# (`./scripts/test.sh`) or let CI run the exact same script (.github/workflows/tests.yml).
# No logic should live only in the workflow YAML; the YAML just provisions Postgres/
# Python/Node and then calls this.
#
# Three stages, each must pass before the next starts:
#   1. Python test suite (pytest) — unit + cross-surface (cli/api/mcp) + migration.
#   2. Frontend build (npm run build) — the dashboard must compile cleanly.
#   3. Live end-to-end smoke — spawns the REAL `python -m agent_memory.server`
#      subprocess (reading env vars exactly like production) serving the just-built
#      dashboard, and curls it. This is the layer that would have caught, this
#      session alone: a stale default port in __main__.py, a store method missing
#      from its own return statement, and the SPA/API route collision on F5 — none
#      of which the in-process pytest suite exercises, since it never runs the real
#      __main__ entrypoint or serves real static files from disk.
#
# Requires: AGENT_MEMORY_TEST_PG_DSN pointing at a scratch, truncatable Postgres.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '\n\033[1;36m▶ %s\033[0m\n' "$1"; }
ok()  { printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[1;31m✗ %s\033[0m\n' "$1"; exit 1; }

if [ -z "${AGENT_MEMORY_TEST_PG_DSN:-}" ]; then
  cat >&2 <<'EOF'
✗ AGENT_MEMORY_TEST_PG_DSN is not set.

Point it at a scratch, truncatable Postgres database, e.g.:
  podman run -d --name memtest -e POSTGRES_USER=memory -e POSTGRES_PASSWORD=memory \
    -e POSTGRES_DB=memory_test -p 5433:5432 docker.io/library/postgres:16
  export AGENT_MEMORY_TEST_PG_DSN=postgresql://memory:memory@localhost:5433/memory_test
EOF
  exit 1
fi

# ---- 1. Python test suite --------------------------------------------------
log "1/3 Python test suite (pytest)"
python -m pytest -q
ok "pytest passed"

# ---- 2. Frontend build ------------------------------------------------------
log "2/3 Frontend build (npm run build)"
( cd web && npm ci --no-audit --no-fund && npm run build )
ok "frontend build passed"

# ---- 3. Live end-to-end smoke ----------------------------------------------
log "3/3 Live end-to-end smoke (real server subprocess)"

PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()")
TOKEN="e2e-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
BASE="http://127.0.0.1:${PORT}"

export AGENT_MEMORY_DB="$AGENT_MEMORY_TEST_PG_DSN"
export AGENT_MEMORY_API_TOKEN="$TOKEN"
export AGENT_MEMORY_STATIC_DIR="$ROOT/web/dist"
export AGENT_MEMORY_HOST="127.0.0.1"
export AGENT_MEMORY_PORT="$PORT"

# Own clean slate: step 1 leaves the test DB schema'd via raw create_all (no
# Alembic version row), so blindly running `alembic upgrade head` on top of it
# would try to recreate tables that already exist. Wiping first also means this
# stage genuinely proves the real bootstrap sequence (fresh DB -> migrate -> boot),
# not just "whatever state pytest happened to leave" — the DB is scratch-only, so
# discarding step 1's leftover rows here is fine.
python -c "
import asyncio
from sqlalchemy import text
from agent_memory.server.db import make_engine
async def main():
    eng = make_engine()
    async with eng.begin() as c:
        await c.execute(text('DROP SCHEMA public CASCADE'))
        await c.execute(text('CREATE SCHEMA public'))
    await eng.dispose()
asyncio.run(main())
"
python -m alembic upgrade head >/dev/null

python -m agent_memory.server &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true' EXIT

deadline=$((SECONDS + 20))
until curl -sf "$BASE/health" >/dev/null 2>&1; do
  if [ "$SECONDS" -ge "$deadline" ]; then fail "server did not become healthy in time"; fi
  sleep 0.3
done
ok "server started (pid $SERVER_PID, port $PORT)"

status() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

[ "$(status "$BASE/health")" = "200" ] && ok "/health -> 200" || fail "/health did not return 200"

[ "$(status "$BASE/memories")" = "401" ] && ok "/memories without token -> 401" \
  || fail "/memories without a token should be 401"

[ "$(status -H "Authorization: Bearer $TOKEN" "$BASE/memories")" = "200" ] \
  && ok "/memories with token -> 200" || fail "/memories with a valid token should be 200"

[ "$(status "$BASE/tags")" = "401" ] && ok "/tags (bare API path) without token -> 401" \
  || fail "/tags without a token should be 401 — it must stay a real protected API route"

[ "$(status "$BASE/app")" = "200" ] && ok "/app (SPA root) -> 200" \
  || fail "/app should serve the dashboard"

[ "$(status "$BASE/app/tags")" = "200" ] && ok "/app/tags (SPA deep route, F5-safe) -> 200" \
  || fail "/app/tags should serve the dashboard (SPA fallback regression)"

[ "$(status "$BASE/app/totally/unmatched/nested/path")" = "200" ] \
  && ok "unmatched deep path -> 200 (SPA fallback catches it)" \
  || fail "an arbitrary unmatched path under the SPA should still serve index.html"

DASH_TITLE=$(curl -s "$BASE/app" | grep -o '<title>[^<]*</title>' || true)
[ -n "$DASH_TITLE" ] && ok "dashboard HTML served ($DASH_TITLE)" || fail "no <title> found in /app response"

log "All checks passed ✓"
