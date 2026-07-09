<script setup>
// D5 — multi-tag filter (AND semantics). Lives in the right rail: lists every
// tag with its count (from `tags.list`). Clicking a tag PROMOTES it out of the
// rail into a highlighted active-filter bar teleported to the top of the
// memories area; selected tags leave the rail list. Removing a chip demotes it
// back. Selected tags drive `memories.tags` (AND) and round-trip to the URL via
// the store, so we keep NO local copy of the selection.
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useMemoriesStore } from '@/stores/memories'
import { useTagsStore } from '@/stores/tags'

const memories = useMemoriesStore()
const tags = useTagsStore()
const { tags: selected } = storeToRefs(memories)

// Rail shows only tags that aren't already promoted into the active bar.
const available = computed(() =>
  tags.list.filter((t) => !selected.value.includes(t.name)),
)

// Active tags, ordered as the tags store lists them (with live counts).
const active = computed(() =>
  selected.value.map(
    (name) => tags.list.find((t) => t.name === name) || { name, count: null },
  ),
)

function add(name) {
  if (selected.value.includes(name)) return
  memories.tags = [...selected.value, name]
  memories.setPage(1)
  memories.fetch()
}

function remove(name) {
  memories.tags = selected.value.filter((n) => n !== name)
  memories.setPage(1)
  memories.fetch()
}

function clearAll() {
  if (!selected.value.length) return
  memories.tags = []
  memories.setPage(1)
  memories.fetch()
}
</script>

<template>
  <!-- ===== Promoted active-filter bar (top of the memories area) ===== -->
  <Teleport to="#active-filters-target" defer>
    <div v-if="active.length" class="q-mb-md row items-center q-gutter-xs">
      <span class="text-caption text-grey q-mr-xs">Filtering by</span>
      <q-chip
        v-for="t in active"
        :key="t.name"
        removable
        color="primary"
        text-color="white"
        class="text-weight-bold"
        @remove="remove(t.name)"
      >
        {{ t.name }}
        <span v-if="t.count != null" class="q-ml-xs text-caption">({{ t.count }})</span>
      </q-chip>
      <q-btn
        v-if="active.length > 1"
        flat
        dense
        no-caps
        size="sm"
        label="Clear all"
        @click="clearAll"
      />
    </div>
  </Teleport>

  <!-- ===== Rail: available tags ===== -->
  <div class="row items-center justify-between q-mb-sm">
    <div class="text-subtitle2">Filter by tag</div>
    <q-badge v-if="active.length" color="primary" :label="active.length" />
  </div>
  <div class="text-caption text-grey q-mb-sm">
    Click to add — memories must match <b>all</b> selected tags.
  </div>

  <div class="column q-gutter-xs">
    <q-chip
      v-for="t in available"
      :key="t.name"
      clickable
      outline
      color="grey-8"
      class="justify-between full-width q-ml-none"
      @click="add(t.name)"
    >
      <span>{{ t.name }}</span>
      <q-badge color="grey-6" :label="t.count" class="q-ml-sm" />
    </q-chip>
    <div v-if="!available.length && !tags.list.length" class="text-caption text-grey">
      No tags.
    </div>
    <div v-else-if="!available.length" class="text-caption text-grey">
      All tags selected.
    </div>
  </div>
</template>
