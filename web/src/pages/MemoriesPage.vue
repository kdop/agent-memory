<script setup>
// Memories view. Wires the data flow (hydrate view-state from the URL, then
// fetch) and mounts the ticket components into their regions:
//   SearchBar           — top of the view
//   #active-filters     — promoted tag chips (filled by TagFilter via teleport)
//   MemoriesTable       — paginated table + create/edit/delete
//   TagFilter           — right rail (teleported), promotes chips up here
import { onMounted } from 'vue'
import { useMemoriesStore } from '@/stores/memories'
import { useTagsStore } from '@/stores/tags'
import { useAuthStore } from '@/stores/auth'
import { useUrlSync } from '@/composables/useUrlSync'
import SearchBar from '@/components/SearchBar.vue'
import MemoriesTable from '@/components/MemoriesTable.vue'
import TagFilter from '@/components/TagFilter.vue'
import AgentProjectFilter from '@/components/AgentProjectFilter.vue'

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
    <!-- search -->
    <div class="q-mb-md">
      <SearchBar />
    </div>

    <!-- memories q-table (+ create/edit/delete) -->
    <MemoriesTable />

    <!-- multi-tag filter (right rail). `defer` so the teleport waits for the
         Quasar drawer's target to exist in the DOM before mounting into it. -->
    <Teleport to="#right-rail-target" defer>
      <AgentProjectFilter />
      <TagFilter />
    </Teleport>
  </q-page>
</template>
