<script setup>
// Search bar: the debounced input drives `memories.q`, the mode selector next
// to it drives `memories.mode` (keyword, semantic, hybrid). Results carry a
// `snippet` for keyword hits and a `score` for the ranked modes; the table
// renders both. Keeps NO local copy of the query or the mode — the store is
// the single source of truth so the URL (via useUrlSync) stays in step.
import { storeToRefs } from 'pinia'
import { useMemoriesStore, SEARCH_MODES } from '@/stores/memories'

const memories = useMemoriesStore()
const { q, mode, fallback } = storeToRefs(memories)

const MODE_OPTIONS = SEARCH_MODES.map((m) => ({ label: m, value: m }))

/** Apply a new query: reset to page 1 (offset 0) then refetch. */
function onSearch(value) {
  memories.q = value || ''
  memories.setPage(1)
  memories.fetch()
}

/** Switch the mode. Only refetches when there is a query to run it on. */
function onMode(value) {
  memories.mode = value || 'keyword'
  memories.setPage(1)
  if (memories.q) memories.fetch()
}
</script>

<template>
  <div class="row items-start q-col-gutter-sm">
    <div class="col">
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
      <!-- The server could not run the mode asked for and served another. -->
      <div v-if="fallback" class="text-caption text-warning q-mt-xs" data-test="search-fallback">
        <q-icon name="info" size="xs" class="q-mr-xs" />
        The server has no embedding model, so it answered with {{ fallback }} search instead.
      </div>
    </div>
    <div class="col-auto">
      <q-btn-toggle
        :model-value="mode"
        :options="MODE_OPTIONS"
        dense
        no-caps
        unelevated
        toggle-color="primary"
        color="grey-4"
        text-color="grey-9"
        aria-label="Search mode"
        @update:model-value="onMode"
      />
    </div>
  </div>
</template>
