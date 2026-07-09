# Frozen API contract — dashboard v1

The dashboard talks to the FastAPI memory server. **This document is the frozen
contract**: the UI swarm builds against it (via the MSW mock in `web/src/mocks/`), and
the backend (D1) implements it. Do not diverge without updating this file.

Base URL comes from config (dev: Vite proxy / MSW; prod: same-origin `/`).

## Auth

Every endpoint below requires `Authorization: Bearer <token>` **except** `GET /health`
and the static assets. On a missing/invalid token the server returns `401`; a server
with no token configured returns `503`. The dashboard stores the token in `localStorage`
and validates it on login by calling `GET /tags`.

## Types

```jsonc
// MemoryOut
{
  "id": 42,
  "timestamp": "2026-07-09 14:50:41+00:00",   // string, may be null
  "agent": "agent-a",
  "project": "agent-memory",                    // may be null
  "content": "…",
  "type": "decision",                           // may be null
  "tags": ["auth", "db"],                       // tag NAMES, alphabetical
  "snippet": "→match← …"                        // only present on search (q=), else null
}

// TagCount
{ "name": "auth", "count": 12, "description": "authentication flow" }

// TagIn (request)   — description optional; blank → server defaults new tag to its name
{ "name": "auth", "description": "authentication flow" }
```

## Memories

### `GET /memories` — list / search / filter (paginated)
Query params (all optional):

| param | type | notes |
|---|---|---|
| `q` | string | full-text search; when present results carry `snippet` and are ranked |
| `tag` | string (repeatable) | **AND** — a memory must have *every* listed tag. `?tag=a&tag=b` |
| `project` | string | exact |
| `agent` | string | exact |
| `type` | string | exact |
| `since_days` | int | single day N days ago (0=today); overrides since/until |
| `since` / `until` | string | ISO date/datetime bounds |
| `order` | `date_desc`\|`date_asc` | default `date_desc` (ignored when `q` set → rank order) |
| `limit` | int | default **100** |
| `offset` | int | default 0 |

**Response `200`**: `MemoryOut[]`, plus header **`X-Total-Count: <int>`** = total matches
ignoring limit/offset (drives pagination). Example:
```
GET /memories?tag=auth&tag=db&order=date_desc&limit=100&offset=0
→ 200, X-Total-Count: 37
[ {MemoryOut}, … ]
```

### `POST /memories` — create
Body: `{ "content": str, "agent"?: str, "project"?: str, "type"?: str, "tags"?: TagIn[] }`
**`201`** → `{ "id": 42 }`. Empty/blank tag name → `422`.

### `GET /memories/bulk?ids=1&ids=2` — compact rows
**`200`** → `[{ "id", "agent", "project", "type", "content" }]`.

### `GET /memories/{id}`
**`200`** → `MemoryOut`; **`404`** if absent.

### `PATCH /memories/{id}` — edit (only sent fields apply)
Body (any subset): `{ "content"?, "project"?, "type"?, "set_tags"?: TagIn[],
"add_tags"?: TagIn[], "remove_tags"?: string[] }`.
`project`/`type` = `""` clears; `set_tags` = `[]` removes all.
**`200`** → `{ "changes": ["content", "+tags: x", …] }`; **`404`** if absent.

### `DELETE /memories?ids=1&ids=2`
**`200`** → `{ "deleted": 1, "missing": [2] }`.

## Tags

### `GET /tags`
**`200`** → `TagCount[]`. (Client sorts: alphabetical by name default.)

### `PATCH /tags/{name}` — rename and/or re-describe
Body: `{ "name"?: str, "description"?: str }`. If `name` collides with an existing tag
(case-insensitive), the two **merge** (see merge semantics).
**`200`** → `{ "name", "description", "count" }`; **`404`** if the tag is absent.

### `DELETE /tags/{name}` — delete a tag and all its links
**`200`** → `{ "removed": "auth", "memories_affected": 12 }`; **`404`** if absent.

### `POST /tags/merge` — consolidate N sources → 1 target
Body: `{ "sources": ["authn","auth2"], "target": "auth", "description"?: str }`.
Every memory tagged with a source gets `target` instead; sources are deleted; `target`
is created if missing and keeps `description` when provided (else its existing one).
**`200`** → `{ "target": "auth", "memories_affected": 20, "removed": ["authn","auth2"] }`.

### `POST /tags/{name}/detach` — remove a tag from some/all memories
Body: `{ "memory_ids"?: int[] }` — omit or `[]` = **all** memories. The tag itself stays.
**`200`** → `{ "detached": 8 }`; **`404`** if the tag is absent.

## Misc (unchanged from the api-first server)

- `GET /projects` → `[{ "project", "count" }]`
- `GET /stats` → `{ "total", "agents", "projects", "tags", "today", "week", "oldest", "newest" }`
- `GET /health` → `{ "status": "ok" }` (no auth)

## Mock seed (for `web/src/mocks/`)

Seed the mock with ~250 memories across agents `agent-a`/`clu`, projects
`agent-memory`/`project-a`/null, types `decision`/`code`/`lesson`/`note`, and ~40 tags with
descriptions and realistic counts, timestamps spread over the last ~60 days — enough to
exercise pagination (3 pages), search, AND-filtering, and tag merge.
