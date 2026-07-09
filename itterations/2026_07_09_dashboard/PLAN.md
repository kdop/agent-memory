# Dashboard iteration — plan (2026-07-09)

A sqlite-web-style web dashboard for the memory system. Rides on the **API-first** epic
(the FastAPI server is the backend). Contract-first: freeze the API, then swarm the UI
against a mock while the backend lands.

## Decisions (from Q&A)

- **Multi-tag filter = AND** (selecting more tags narrows).
- **Vue 3 + Quasar** (Vite build, served as static files by FastAPI). `q-table` gives
  pagination / date-sort / in-place edit; `q-dialog` for the tag modal.
- **Auth = paste-token login**, stored in `localStorage`, sent as the bearer header.
  Static assets served unauthenticated; every API call carries the token.
- **Tag merge = N sources → 1 target**: reassign every memory from a source tag to the
  target (create if missing), delete the sources; target keeps its description
  (editable in the merge dialog).
- Selected-tag UX: chosen tags are **promoted to a highlighted filter bar at the top**
  (bold chip + `×`); unselected tags stay in the right rail. (my call)
- Tag detail (2c) = **modal**, not a separate page → single-page app, no client router.
- URL state via **query params** (`?q=&tags=a,b&order=date_desc&page=2`) — copy/refresh
  restores the exact view (1e).
- In-place edits that fall outside the current filter **stay until refresh** (eventual
  consistency, 1d) — the client does not re-filter optimistic edits.

## Frozen API contract (v1) — additions over the api-first server

Bearer token on all except `GET /health` + the static assets.

**Memories** (unified list endpoint):
- `GET /memories` — params: `q` (full-text; when set → snippet + rank order, else date
  order), `tag` (repeatable, **AND**), `project`, `agent`, `type`, `since_days|since|until`,
  `order=date_desc|date_asc` (default `date_desc`), `limit` (default 100), `offset`.
  Body: `[MemoryOut]`; header `X-Total-Count` = total matches (for pagination).
  (Folds in the old `/memories/search`; `q` drives search.)
- `POST /memories` (create), `GET /memories/{id}`, `PATCH /memories/{id}`
  (content/project/type + set/add/remove tags), `DELETE /memories?ids=` (→ {deleted,
  missing}), `GET /memories/bulk?ids=` — all as in the api-first server.

**Tags**:
- `GET /tags` — `[{name, count, description}]` (client sorts).
- `PATCH /tags/{name}` — `{name?, description?}`: rename (collision → merge into existing)
  and/or re-describe.
- `DELETE /tags/{name}` — remove tag + its links → `{removed, memories_affected}`.
- `POST /tags/merge` — `{sources:[name], target:name, description?}` → `{target,
  memories_affected, removed:[…]}`.
- `POST /tags/{name}/detach` — `{memory_ids?:[int]}` (omit = all) → `{detached}`.

**Dashboard**: `GET /` + static assets (Quasar `dist/`). Login validates the token by
calling an authed endpoint (`GET /tags`).

Unchanged: `GET /projects`, `GET /stats`, `GET /health`.

## Tickets (Dashboard epic)

Backend (extends the api-first server):
- **D1** — API extensions: unified `GET /memories` (q + multi-tag AND + offset +
  `X-Total-Count` + order), tag management (`PATCH`/`DELETE /tags/{name}`,
  `POST /tags/merge`, `POST /tags/{name}/detach`), SPA static mount. **Freeze OpenAPI.**

Frontend foundation:
- **D2** — Vue 3 + Quasar scaffold (Vite): token-login (localStorage) + API client +
  shared memories store (view state ↔ URL query params) + two-pane layout shell +
  a contract mock for offline dev. *(Everything else builds on this.)*

Frontend, parallel (swarm after D1 contract + D2 skeleton):
- **D3** — Memories `q-table`: chronological default, date-sortable column, 100/page
  pagination ↔ URL.
- **D4** — Search field (top) ↔ `q` ↔ URL.
- **D5** — Right-rail multi-tag filter: promote selected → highlighted chips w/ `×`,
  AND semantics, ↔ URL.
- **D6** — In-place row edit (content/project/type/tags) via `PATCH`, optimistic +
  eventual consistency; create + delete memory.
- **D7** — Tags view: alphabetical, name-sortable, rename / re-describe / delete.
- **D8** — Tag detail modal: memories for a tag, detach-from-some/all, merge dialog.

Integration:
- **D9** — Build → serve from FastAPI, e2e smoke, docs.

## Parallelization

1. **D1** (me, on the api-first branch) → freeze the OpenAPI contract.
2. **D2** skeleton (defines the shared Pinia store + URL-state util + API client + mock)
   so D3–D8 plug in without colliding.
3. **Swarm D3–D8** in parallel worktrees against the mock; I finish the real backend
   (api-first client/tests + D1 endpoints) concurrently.
4. **D9** integrate against the real API, build, serve, smoke, docs.

Depends on the api-first epic (server/repo). Finish that foundation first (nearly done),
then D1 layers on.

## Open risk

Node/npm toolchain must be available for the Vite/Quasar build and for swarm agents to
build+test the UI — verify before swarming.
