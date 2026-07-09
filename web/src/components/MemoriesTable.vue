<script setup>
// D3 — paginated memories table (server-side) + D6 create / edit / delete.
//
// Server-side pagination & sort are driven entirely by the `memories` store
// (results / total / order / loading + setPage). We bind `:pagination` from a
// computed view of the store and react to q-table's `@request` — no local copy
// of page/sort state. Only the Date column is sortable (→ memories.order).
//
// Mutations (D6):
//   • edit   → PATCH, then OPTIMISTICALLY patch the row in place (no refetch —
//              an edit that no longer matches the filter stays until refresh).
//   • create → POST, then memories.fetch().
//   • delete → DELETE (confirm), then memories.fetch().
import { computed, ref } from 'vue'
import { useQuasar } from 'quasar'
import { useMemoriesStore } from '@/stores/memories'
import { api } from '@/api/client'
import MemoryEditDialog from './MemoryEditDialog.vue'

const $q = useQuasar()
const memories = useMemoriesStore()

const columns = [
  { name: 'id', label: 'ID', field: 'id', align: 'left', style: 'width: 60px' },
  { name: 'date', label: 'Date', field: 'timestamp', align: 'left', sortable: true, format: (v) => fmtDate(v) },
  { name: 'agent', label: 'Agent', field: 'agent', align: 'left' },
  { name: 'project', label: 'Project', field: (r) => r.project || '—', align: 'left' },
  { name: 'type', label: 'Type', field: (r) => r.type || '—', align: 'left' },
  { name: 'content', label: 'Content', field: 'content', align: 'left' },
  { name: 'tags', label: 'Tags', field: 'tags', align: 'left' },
  { name: 'actions', label: '', field: 'actions', align: 'right', style: 'width: 90px' },
]

// q-table pagination shape, derived read-only from the store.
const pagination = computed(() => ({
  sortBy: 'date',
  descending: memories.order === 'date_desc',
  page: memories.page,
  rowsPerPage: memories.limit,
  rowsNumber: memories.total,
}))

/** q-table server-side driver: user paged or toggled the Date sort. */
function onRequest(props) {
  const { page, sortBy, descending } = props.pagination
  let targetPage = page
  if (sortBy === 'date') {
    const nextOrder = descending ? 'date_desc' : 'date_asc'
    if (nextOrder !== memories.order) {
      memories.order = nextOrder
      targetPage = 1 // new sort → back to first page
    }
  }
  memories.setPage(targetPage)
  memories.fetch()
}

// ---- date formatting ----------------------------------------------------
function fmtDate(ts) {
  if (!ts) return '—'
  const d = new Date(String(ts).replace(' ', 'T'))
  if (isNaN(d)) return ts
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// ---- search snippet highlight -------------------------------------------
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}
/** Turn a server snippet ("…→match←…") into safe highlighted HTML. */
function renderSnippet(snippet) {
  return escapeHtml(snippet).replace(/→(.*?)←/g, '<mark>$1</mark>')
}

// ---- create / edit / delete (D6) ----------------------------------------
const dialogOpen = ref(false)
const editing = ref(null) // null → create mode

function openCreate() {
  editing.value = null
  dialogOpen.value = true
}
function openEdit(row) {
  editing.value = row
  dialogOpen.value = true
}

async function onSubmit(payload) {
  try {
    if (payload.isEdit) {
      await api.patchMemory(payload.id, {
        content: payload.content,
        project: payload.project,
        type: payload.type,
        set_tags: payload.tagNames.map((name) => ({ name })),
      })
      // Optimistic in-place update; deliberately NO refetch (eventual consistency).
      memories.patchRow(payload.id, {
        content: payload.content,
        project: payload.project || null,
        type: payload.type || null,
        tags: [...payload.tagNames].sort(),
      })
      $q.notify({ type: 'positive', message: `Memory #${payload.id} updated` })
    } else {
      const { id } = await api.createMemory({
        content: payload.content,
        agent: payload.agent || undefined,
        project: payload.project || undefined,
        type: payload.type || undefined,
        tags: payload.tagNames.map((name) => ({ name })),
      })
      await memories.fetch()
      $q.notify({ type: 'positive', message: `Memory #${id} created` })
    }
  } catch (err) {
    $q.notify({ type: 'negative', message: err?.message || 'Save failed' })
  }
}

function confirmDelete(row) {
  $q.dialog({
    title: 'Delete memory',
    message: `Delete memory #${row.id}? This cannot be undone.`,
    cancel: true,
    persistent: true,
    ok: { label: 'Delete', color: 'negative' },
  }).onOk(async () => {
    try {
      await api.deleteMemories([row.id])
      await memories.fetch()
      $q.notify({ type: 'positive', message: `Memory #${row.id} deleted` })
    } catch (err) {
      $q.notify({ type: 'negative', message: err?.message || 'Delete failed' })
    }
  })
}
</script>

<template>
  <q-table
    flat
    bordered
    row-key="id"
    :rows="memories.results"
    :columns="columns"
    :loading="memories.loading"
    :pagination="pagination"
    :rows-per-page-options="[100]"
    binary-state-sort
    wrap-cells
    @request="onRequest"
  >
    <!-- Toolbar: count + New memory -->
    <template #top>
      <div class="text-subtitle2">
        {{ memories.total }} memories<span v-if="memories.q"> matching “{{ memories.q }}”</span>
      </div>
      <q-space />
      <q-btn color="primary" icon="add" no-caps label="New memory" @click="openCreate" />
    </template>

    <!-- Content: snippet-highlighted when searching, else clamped preview -->
    <template #body-cell-content="props">
      <q-td :props="props" style="max-width: 420px; min-width: 260px">
        <div
          v-if="props.row.snippet"
          class="mem-snippet"
          v-html="renderSnippet(props.row.snippet)"
        />
        <div v-else class="mem-content">{{ props.row.content }}</div>
      </q-td>
    </template>

    <!-- Tags: chips -->
    <template #body-cell-tags="props">
      <q-td :props="props">
        <q-chip
          v-for="t in props.row.tags"
          :key="t"
          dense
          size="sm"
          color="grey-3"
          text-color="grey-9"
          class="q-ma-none q-mr-xs"
        >
          {{ t }}
        </q-chip>
        <span v-if="!props.row.tags.length" class="text-grey">—</span>
      </q-td>
    </template>

    <!-- Row actions -->
    <template #body-cell-actions="props">
      <q-td :props="props" @click.stop>
        <q-btn flat dense round icon="edit" size="sm" aria-label="Edit"
               @click="openEdit(props.row)" />
        <q-btn flat dense round icon="delete" size="sm" color="negative" aria-label="Delete"
               @click="confirmDelete(props.row)" />
      </q-td>
    </template>

    <template #no-data>
      <div class="full-width row flex-center text-grey q-py-md">No memories.</div>
    </template>
  </q-table>

  <MemoryEditDialog v-model="dialogOpen" :memory="editing" @submit="onSubmit" />
</template>

<style scoped>
.mem-content,
.mem-snippet {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: normal;
  word-break: break-word;
}
.mem-snippet :deep(mark) {
  background: var(--q-primary);
  color: #fff;
  padding: 0 2px;
  border-radius: 2px;
}
</style>
