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

## Refreshing the README screenshot

How `docs/dashboard.png` was produced (2026-09-15):

1. Run `npm run dev` in `web/` (MSW mock, 250 seeded memories, any non-empty token is
   accepted).
2. Serve a wrapper page from `web/public/` that sets `localStorage`
   `agent-memory.token` and `mem-dark` = `true`, embeds `/app` in a full-size iframe,
   and includes an `<img>` from a local HTTP server that sleeps ~8 s before answering.
   Firefox headless has no delay flag; the slow image holds the page `load` event until
   the SPA has rendered.
3. Take the shot:

   ```bash
   firefox --headless --no-remote --profile $(mktemp -d) --window-size=1440,900 \
     --screenshot out.png http://127.0.0.1:5173/wrapper.html
   ```

If the dev server runs from a git worktree, `node_modules` must be a real copy: a
symlink makes Vite refuse the font files (outside its allow list) and icons render as
text.

## Layout

```
src/
  api/client.js        # typed client for the whole CONTRACT.md
  stores/              # auth · memories (view-state ↔ URL) · tags
  composables/useUrlSync.js   # q/tags/order/page ↔ query params
  layouts/MainLayout.vue      # top bar (+ review model badge) + right rail
  pages/               # MemoriesPage · FlaggedPage · TagsPage
  components/          # MemoriesTable · SearchBar · TagFilter · AgentProjectFilter
                       # · MemoryEditDialog · TagsTable · TagDetailModal · LoginDialog
  mocks/               # MSW handlers + deterministic seed (dev only)
```
