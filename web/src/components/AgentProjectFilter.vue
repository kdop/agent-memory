<script setup>
// Right-rail exact-match filters: agent and project. Options come from the
// dedicated /agents and /projects endpoints (the full distinct set, not just
// what's on the current page). Drives `memories.agent` / `memories.project`
// (both round-trip to the URL via useUrlSync) — no local copy of the selection.
import { onMounted, ref } from 'vue'
import { useMemoriesStore } from '@/stores/memories'
import { api } from '@/api/client'

const memories = useMemoriesStore()

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
      @update:model-value="apply"
    />
  </div>
  <q-separator class="q-mb-md" />
</template>
