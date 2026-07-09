<script setup>
// Memories view. Owns three regions built out by other tickets (marked below).
// This skeleton wires the data flow: hydrate view-state from the URL, then fetch.
import { onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useMemoriesStore } from '@/stores/memories'
import { useTagsStore } from '@/stores/tags'
import { useAuthStore } from '@/stores/auth'
import { useUrlSync } from '@/composables/useUrlSync'

const memories = useMemoriesStore()
const tags = useTagsStore()
const auth = useAuthStore()
const { results, total, loading, q, order } = storeToRefs(memories)

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
    <!-- ============ D4: search ============
      Replace with the search bar bound to `memories.q` (v-model) that calls
      memories.fetch() on submit. Placeholder shows the wiring is live. -->
    <div class="q-mb-md">
      <!-- D4: search -->
      <q-input
        v-model="q"
        dense outlined clearable
        placeholder="Search memories (D4 owns this)…"
        @keyup.enter="memories.fetch()"
        @clear="memories.fetch()"
      >
        <template #prepend><q-icon name="search" /></template>
      </q-input>
    </div>

    <!-- ============ D3: memories q-table ============
      Replace with the paginated q-table bound to `memories.results` /
      `memories.total`, driving `memories.order`, `memories.setPage()`, and
      `memories.loading`. -->
    <!-- D3: memories q-table -->
    <q-card flat bordered>
      <q-card-section class="row items-center justify-between">
        <div class="text-subtitle2">
          {{ total }} memories<span v-if="q"> matching “{{ q }}”</span>
        </div>
        <q-btn dense flat no-caps
               :label="order === 'date_desc' ? 'Newest first' : 'Oldest first'"
               icon="swap_vert"
               @click="order = order === 'date_desc' ? 'date_asc' : 'date_desc'; memories.fetch()" />
      </q-card-section>
      <q-separator />
      <q-inner-loading :showing="loading" />
      <q-list separator>
        <q-item v-for="m in results" :key="m.id">
          <q-item-section>
            <q-item-label lines="2">{{ m.content }}</q-item-label>
            <q-item-label caption>
              #{{ m.id }} · {{ m.agent }} · {{ m.project || '—' }} ·
              {{ m.type || '—' }} · {{ m.tags.join(', ') }}
            </q-item-label>
          </q-item-section>
        </q-item>
        <q-item v-if="!loading && !results.length">
          <q-item-section class="text-grey">No memories.</q-item-section>
        </q-item>
      </q-list>
    </q-card>

    <!-- ============ D5: multi-tag filter (right rail) ============
      Teleported into MainLayout's right rail. Replace with the multi-select tag
      filter bound to `memories.tags` (array, AND). It should call
      memories.fetch() on change. `tags.list` provides the available tags. -->
    <Teleport to="#right-rail-target">
      <!-- D5: multi-tag filter -->
      <div class="text-subtitle2 q-mb-sm">Filter by tag</div>
      <div class="text-caption text-grey">
        D5 mounts the AND multi-tag selector here. Bound state:
        <code>memories.tags</code> · options: <code>tags.list</code>.
      </div>
    </Teleport>
  </q-page>
</template>
