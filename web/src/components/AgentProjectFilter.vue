<script setup>
// Right-rail filters: agent, project, type, review status and the current-only
// switch. Agent and project options come from the dedicated /agents and
// /projects endpoints (the full distinct set, not just what's on the current
// page). Drives `memories.agent` / `memories.project` / `memories.type` / `memories.status` /
// `memories.current` (all round-trip to the URL via useUrlSync) — no local
// copy of the selection.
import { onMounted, ref } from 'vue'
import { useMemoriesStore, REVIEW_STATUSES, MEMORY_TYPES } from '@/stores/memories'
import { api } from '@/api/client'

const memories = useMemoriesStore()

const statusOptions = REVIEW_STATUSES.map((s) => ({ label: s, value: s }))
const typeOptions = MEMORY_TYPES.map((t) => ({ label: t, value: t }))
const agentOptions = ref([])   // [{ label, value, count }]
const projectOptions = ref([])
const loading = ref(false)

onMounted(async () => {
  loading.value = true
  try {
    const [agents, projects] = await Promise.all([api.listAgents(), api.listProjects()])
    agentOptions.value = agents.map((a) => ({ label: `${a.agent} (${a.count})`, value: a.agent }))
    projectOptions.value = projects.map((p) => ({ label: `${p.project} (${p.count})`, value: p.project }))
  } finally {
    loading.value = false
  }
})

function apply() {
  memories.setPage(1)
  memories.fetch()
}
</script>

<template>
  <div class="q-mb-md">
    <div class="text-subtitle2 q-mb-sm">Filter by</div>
    <q-select
      v-model="memories.agent"
      :options="agentOptions"
      label="Agent"
      dense
      outlined
      clearable
      emit-value
      map-options
      :loading="loading"
      class="q-mb-sm"
      @update:model-value="apply"
    />
    <q-select
      v-model="memories.project"
      :options="projectOptions"
      label="Project"
      dense
      outlined
      clearable
      emit-value
      map-options
      :loading="loading"
      class="q-mb-sm"
      @update:model-value="apply"
    />
    <!-- Type: one of the five, or any. Like status, not applied to semantic
         and hybrid search. -->
    <q-select
      v-model="memories.type"
      :options="typeOptions"
      label="Type"
      dense
      outlined
      clearable
      emit-value
      map-options
      :disable="memories.ranked"
      class="q-mb-sm"
      @update:model-value="apply"
    />
    <!-- Review status: one of the three, or any. Not applied to semantic and
         hybrid search, which the server ranks without a status filter. -->
    <q-select
      v-model="memories.status"
      :options="statusOptions"
      label="Status"
      dense
      outlined
      clearable
      emit-value
      map-options
      :disable="memories.ranked"
      class="q-mb-sm"
      @update:model-value="apply"
    />
    <!-- Hide the memories a newer one supersedes (sends current=true). -->
    <q-toggle
      v-model="memories.current"
      label="Current only"
      dense
      @update:model-value="apply"
    />
  </div>
  <q-separator class="q-mb-md" />
</template>
