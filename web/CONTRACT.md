# Frozen API contract — dashboard v1

The dashboard talks to the FastAPI memory server. **This document is the frozen
contract**: the frontend builds against it (via the MSW mock in `web/src/mocks/`) and
the backend implements it. Do not diverge without updating this file.

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
  "agent": "my-agent",
  "project": "agent-memory",                    // may be null
  "content": "…",
  "type": "decision",                           // constraint | decision | lesson | note | preference, or null
  "tags": ["auth", "db"],                       // tag NAMES, alphabetical
  "snippet": "→match← …",                       // only present on search (q=), else null
  "score": 0.42,                                // only on GET /memories/search, else null
  "review_status": "unverified",                // unverified | verified | flagged
  "review": ReviewOut,                          // null until the review model has answered
  "supersedes": 12,                             // older memory this one replaces, or null
  "superseded_by": 57                           // newer memory that replaces this one, or null
}

// ReviewOut — what the review model said about the memory
{
  "verdict": "rewrite",                         // approve | reject | rewrite
  "rule": 3,                                    // the rule it breaks; null for approve
  "reason": "…",                                // one sentence
  "rewrite": "…",                               // suggested text; only for rewrite, else null
  "duplicate_of": 40,                           // the memory this one repeats, or null
  "tags": ["auth"],                             // suggested tags; empty unless rewrite
  "supersedes": 12                              // same value as the memory's supersedes
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
| `type` | string | exact: `constraint`\|`decision`\|`lesson`\|`note`\|`preference` |
| `status` | `unverified`\|`verified`\|`flagged` | one review status |
| `current` | bool | `true` hides the memories a newer one supersedes |
| `since_days` | int | rolling window: since the start of the day N days ago (0=today, 7=past week); overrides since/until |
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

### `GET /memories/search` — ranked search in one mode
Query params: `q` (required), `mode` = `keyword`|`semantic`|`hybrid` (default `keyword`),
`project`, `agent`, `since`, `tag` (**one** tag), `current` (bool), `limit` (default 20,
0 = all). No `offset`, no `X-Total-Count`: the whole ranked list comes back at once.

**Response `200`**: `MemoryOut[]`, best first, each with `score` (ts_rank for keyword,
cosine for semantic, the fused rank for hybrid). Keyword hits also carry `snippet`.
`semantic` without an embedding model → **`400`** with `detail` saying why. `hybrid`
without one is served as keyword and says so in the header
**`X-Search-Fallback: keyword`**; the dashboard shows that as a note under the search box.

The dashboard sends `keyword` mode to `GET /memories?q=` (it has pages and every filter)
and the other two modes here.

### `GET /memories/flagged` — what the review flagged
Query params: `project`, `verdict` = `reject`|`rewrite` (only that verdict), `status`
(that review status instead of the flagged verdicts, so `status=unverified` lists what
the model has not read yet), `limit` (default 100, 0 = all).

**Response `200`**: `MemoryOut[]` newest review first, each with `review`, plus
**`X-Total-Count`** ignoring the limit.

### `POST /memories` — create
Body: `{ "content": str, "agent"?: str, "project"?: str, "type"?: str, "tags"?: TagIn[] }`
**`201`** → `{ "id": 42, "warnings": [] }`. Empty/blank tag name → `422`. In enforce
mode a review the model refuses → `422` with `detail.reason = "review"` and the verdict.
Any `type` other than the five (`constraint`, `decision`, `lesson`, `note`,
`preference`) → `422`. The table shows the type as a badge: a filled red one for a
`constraint` (a hard rule), an outline one for the rest.

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

## Review

### `POST /admin/review?limit=50` — catch up
Reviews the unverified memories, oldest first, up to `limit` (0 = all), in one
background task after the response.
**`200`** → `{ "scheduled": 12 }`, or `{ "scheduled": 0, "running": true }` when a
catch-up is already running (only one runs at a time). **`503`** when the server has no
review model.

### `POST /admin/review/{id}` — review one memory now
Replaces any earlier verdict. **`200`** → `ReviewOut`; **`404`** if absent; **`502`** when
the model gave no verdict; **`503`** without a model. The dashboard then re-reads
`GET /memories/{id}` to refresh the card.

## Misc (unchanged from the api-first server)

- `GET /projects` → `[{ "project", "count" }]`
- `GET /agents` → `[{ "agent", "count" }]`
- `GET /stats` → `{ "total", "agents", "projects", "tags", "today", "week", "oldest", "newest" }`
- `GET /health` → `{ "status": "ok", "review_model": "reachable", "catch_up": null }` (no
  auth). `review_model` is `reachable`, `unreachable`, or `off` when the review or its poll
  is off; the dashboard shows it in the top bar when present. `catch_up` is
  `{ "total": 100, "done": 10 }` while a catch-up runs (`done` counts the memories whose
  review ended, with a verdict or without), else `null`. The dashboard polls `/health`
  every 3 s while a catch-up runs and every 30 s otherwise, shows a progress bar under
  the top bar ("Reviewing memories: 10 of 100 done, 90 remaining") while `catch_up` is
  not null, and reloads the current list once when it ends.

## Mock seed (for `web/src/mocks/`)

Seed the mock with ~250 memories across agents `alpha`/`beta`, projects
`agent-memory`/`web-app`/null, types `constraint`/`decision`/`lesson`/`note`/`preference`, and ~40 tags with
descriptions and realistic counts, timestamps spread over the last ~60 days — enough to
exercise pagination (3 pages), search, AND-filtering, and tag merge. Most memories are
`verified`, some `flagged` (a mix of `reject` and `rewrite` verdicts), the newest
`unverified`; a few carry `supersedes` / `superseded_by` links. The mock also serves the
search modes (with a `score`), `/memories/flagged` and the two review routes, and
`GET /health` reports `review_model: reachable`. The mock catch-up takes 2 s per memory
and reports its progress as `catch_up`, so the progress bar can be seen: press
**Catch up** on the Flagged page.
