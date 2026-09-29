# `src/components/` — component map

One component per file, Vue 3 `<script setup>`, Quasar components.

| Component | Mounts into | Store state it drives |
|-----------|-------------|-----------------------|
| `LoginDialog.vue` | `MainLayout` | `auth` |
| `MemoriesTable.vue` | `MemoriesPage`, `FlaggedPage` (given `rows`) | `memories.results/total/order/loading`, `memories.setPage()`, `api.reviewMemory` |
| `SearchBar.vue` | `MemoriesPage` | `memories.q`, `memories.mode` → `memories.fetch()` |
| `TagFilter.vue` | `MemoriesPage` right rail | `memories.tags` (array, AND), options from `tags.list` |
| `MemoryEditDialog.vue` | `MemoriesPage` | `api.getMemory/patchMemory/deleteMemories` |
| `TagsTable.vue` | `TagsPage` | `tags.list`, `api.patchTag/deleteTag/mergeTags` |
| `TagDetailModal.vue` | `TagsPage` | `api.patchTag/mergeTags/detachTag`, `tags.fetch()` |
| `ArchivedPage.vue` (page) | router `/app/archived` | its own rows from `api.listMemories({archived: true})`, `api.restoreMemory`, `health.archiveDays` |
| `AgentProjectFilter.vue` | `MemoriesPage` right rail | `memories.agent`, `memories.project`, `memories.status`, `memories.current` |

## Conventions

- Never hardcode API URLs — call `@/api/client` (the `api` object).
- After any mutation, re-run the relevant store `fetch()` to refresh.
- Keep URL-synced view-state (`q`, `mode`, `tags`, `status`, `current`, `order`,
  page) in the `memories` store so `useUrlSync` keeps the URL in step. Don't add
  local copies.
