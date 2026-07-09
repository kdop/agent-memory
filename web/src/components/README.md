# `src/components/` — component ownership

Shared skeleton pieces live here. Each downstream ticket (D3–D8) adds its
component(s) in this folder and mounts them into the extension points already
marked in the pages/layout. Keep one component per file; name it after its
ticket area so parallel work doesn't collide.

| Ticket | Component(s) to add here | Mounts into | Store state it drives |
|--------|--------------------------|-------------|-----------------------|
| —  | `LoginDialog.vue` (exists) | `MainLayout` | `auth` |
| D3 | `MemoriesTable.vue` | `MemoriesPage` `<!-- D3: memories q-table -->` | `memories.results/total/order/loading`, `memories.setPage()` |
| D4 | `SearchBar.vue` | `MemoriesPage` `<!-- D4: search -->` | `memories.q` → `memories.fetch()` |
| D5 | `TagFilter.vue` | `MemoriesPage` right-rail teleport `<!-- D5: multi-tag filter -->` | `memories.tags` (array, AND), options from `tags.list` |
| D6 | (per ticket — e.g. memory detail/edit modal) | `MemoriesPage` | `api.getMemory/patchMemory/deleteMemories` |
| D7 | `TagsTable.vue` | `TagsPage` `<!-- D7: tags table -->` | `tags.list`, `api.patchTag/deleteTag/mergeTags` |
| D8 | `TagDetailModal.vue` | `TagsPage` `<!-- D8: tag detail modal -->` | `api.patchTag/mergeTags/detachTag`, `tags.fetch()` |

## Conventions

- Vue 3 `<script setup>`, Quasar components.
- Never hardcode API URLs — call `@/api/client` (the `api` object).
- After any mutation, re-run the relevant store `fetch()` to refresh.
- Keep URL-synced view-state (`q`, `tags`, `order`, page) in the `memories`
  store so `useUrlSync` keeps the URL in step. Don't add local copies.
