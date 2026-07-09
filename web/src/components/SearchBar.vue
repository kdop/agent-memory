<script setup>
// D4 — full-text search bar. Debounced input drives `memories.q` and refetches.
// Results carry a `snippet` when q is set; the table (D3) renders the highlight.
// Keeps NO local copy of the query — the store is the single source of truth so
// the URL (via useUrlSync) stays in step.
import { storeToRefs } from 'pinia'
import { useMemoriesStore } from '@/stores/memories'

const memories = useMemoriesStore()
const { q } = storeToRefs(memories)

/** Apply a new query: reset to page 1 (offset 0) then refetch. */
function onSearch(value) {
  memories.q = value || ''
  memories.setPage(1)
  memories.fetch()
}
</script>

<template>
  <q-input
    :model-value="q"
    debounce="300"
    dense
    outlined
    clearable
    placeholder="Search memories…"
    aria-label="Search memories"
    @update:model-value="onSearch"
    @clear="onSearch('')"
  >
    <template #prepend><q-icon name="search" /></template>
  </q-input>
</template>
