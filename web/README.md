# Memory dashboard (Vue 3 + Quasar)

A sqlite-web-style dashboard for the agent-memory service. Talks to the FastAPI API
over HTTP (bearer token stored in `localStorage`); the built assets are served by that
same server, so the dashboard and API are same-origin.

## Develop (against the mock)

```bash
cd web
npm install
npm run dev        # http://localhost:5173 — MSW serves the frozen contract (src/mocks/)
```

The dev server runs entirely against an in-memory mock (250 seeded memories, ~40 tags),
so no backend is needed. `web/CONTRACT.md` is the frozen API contract both the mock and
the backend implement.

## Build + serve from the API server (production)

```bash
cd web && npm run build          # → web/dist/
# then run the API server pointing at the built assets:
AGENT_MEMORY_DB=postgresql://…/memory \
AGENT_MEMORY_API_TOKEN=<token> \
AGENT_MEMORY_STATIC_DIR="$PWD/web/dist" \
python -m agent_memory.server    # dashboard at http://127.0.0.1:8099/app
```

The server serves `dist/` as unauthenticated static assets, with the app routes under
`/app` so they can never collide with an API path on a hard refresh; the SPA sends the
bearer token to the API. Log in once by pasting the token — it validates against
`GET /tags` and is remembered in `localStorage`.

## Layout

```
src/
  api/client.js        # typed client for the whole CONTRACT.md
  stores/              # auth · memories (view-state ↔ URL) · tags
  composables/useUrlSync.js   # q/tags/order/page ↔ query params
  layouts/MainLayout.vue      # top bar + right rail
  pages/               # MemoriesPage · TagsPage
  components/          # MemoriesTable · SearchBar · TagFilter · MemoryEditDialog
                       # · TagsTable · TagDetailModal · LoginDialog
  mocks/               # MSW handlers + deterministic seed (dev only)
```
