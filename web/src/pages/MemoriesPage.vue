<script setup>
// Memories view. Wires the data flow (hydrate view-state from the URL, then
// fetch) and mounts the ticket components into their regions:
//   D4 SearchBar        — top of the view
//   #active-filters     — promoted tag chips (filled by D5 via teleport)
//   D3 MemoriesTable    — paginated table + D6 create/edit/delete
//   D5 TagFilter        — right rail (teleported), promotes chips up here
import { onMounted } from 'vue'
import { useMemoriesStore } from '@/stores/memories'
import { useTagsStore } from '@/stores/tags'
import { useAuthStore } from '@/stores/auth'
import { useUrlSync } from '@/composables/useUrlSync'
import SearchBar from '@/components/SearchBar.vue'
import MemoriesTable from '@/components/MemoriesTable.vue'
import TagFilter from '@/components/TagFilter.vue'

const memories = useMemoriesStore()
const tags = useTagsStore()
const auth = useAuthStore()

// URL <-> store view-state sync (see composable for the param contract).
const { hydrate } = useUrlSync()

onMounted(async () => {
  hydrate() // pull q / tags / order / page from the URL first…
  if (!auth.isAuthed) return // login gate will trigger the fetch after auth
  await Promise.all([memories.fetch(), tags.fetch()])
})
</script>

<template>
  <q-page class="q-pa-md">
    <!-- D4: search -->
    <div class="q-mb-md">
      <SearchBar />
    </div>

    <!-- Active tag filters promoted out of the rail by D5 land here. -->
    <div id="active-filters-target"></div>

    <!-- D3: memories q-table (+ D6 create/edit/delete) -->
    <MemoriesTable />

    <!-- D5: multi-tag filter (right rail) -->
    <Teleport to="#right-rail-target">
      <TagFilter />
    </Teleport>
  </q-page>
</template>
