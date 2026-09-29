<script setup>
// Archived view: the memories the review put away, newest archived first,
// from GET /memories?archived=true. The review archives a memory it rejects
// under rule 2 (a diary line) or rule 4 (what git holds), and the older
// memory of a merge (the newer one supersedes it). Each row shows the newest
// review's reason, the days left before the server deletes it (from
// `archive_days` on GET /health) and a Restore button
// (POST /memories/{id}/restore), which puts it back in every list.
import { onMounted, ref } from 'vue'
import { useQuasar } from 'quasar'
import { useAuthStore } from '@/stores/auth'
import { useHealthStore } from '@/stores/health'
import { api } from '@/api/client'

const $q = useQuasar()
const auth = useAuthStore()
const health = useHealthStore()

const DAY_MS = 86_400_000

const rows = ref([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const restoring = ref(new Set()) // ids with a restore call in flight

const columns = [
  { name: 'id', label: 'ID', field: 'id', align: 'left', style: 'width: 60px' },
  { name: 'archived', label: 'Archived', field: 'archived_at', align: 'left', format: (v) => fmtDate(v) },
  { name: 'left', label: 'Days left', field: (r) => daysLeft(r), align: 'right', style: 'width: 90px' },
  { name: 'project', label: 'Project', field: (r) => r.project || '—', align: 'left' },
  { name: 'content', label: 'Content', field: 'content', align: 'left' },
  { name: 'reason', label: 'Reason', field: (r) => r.review?.reason || '', align: 'left' },
  { name: 'actions', label: '', field: 'actions', align: 'right', style: 'width: 110px' },
]

/** Days until the server deletes the memory; null when it keeps archived
 *  memories for good (archive_days 0) or does not say. */
function daysLeft(row) {
  const keep = health.archiveDays
  if (!keep || keep <= 0 || !row.archived_at) return null
  const at = new Date(String(row.archived_at).replace(' ', 'T'))
  if (isNaN(at)) return null
  const left = keep - (Date.now() - at.getTime()) / DAY_MS
  return Math.max(0, Math.ceil(left))
}

function leftLabel(row) {
  const n = daysLeft(row)
  if (n === null) return '—'
  return n === 1 ? '1 day' : `${n} days`
}

async function fetch() {
  loading.value = true
  error.value = ''
  try {
    const { items, total: t } = await api.listMemories({
      archived: true, order: 'archived_desc', limit: 0,
    })
    rows.value = items
    total.value = t
  } catch (err) {
    error.value = detail(err, 'Could not load the archived memories')
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

async function restore(row) {
  restoring.value = new Set([...restoring.value, row.id])
  try {
    const r = await api.restoreMemory(row.id)
    rows.value = rows.value.filter((x) => x.id !== row.id)
    total.value = Math.max(0, total.value - 1)
    $q.notify({
      type: r.restored ? 'positive' : 'info',
      message: r.restored ? `Memory #${row.id} restored` : `Memory #${row.id} was not archived`,
    })
  } catch (err) {
    $q.notify({ type: 'negative', message: detail(err, 'Restore failed') })
  } finally {
    const next = new Set(restoring.value)
    next.delete(row.id)
    restoring.value = next
  }
}

function detail(err, fallback) {
  const d = err?.data?.detail
  return typeof d === 'string' ? d : (err?.message || fallback)
}

function fmtDate(ts) {
  if (!ts) return '—'
  const d = new Date(String(ts).replace(' ', 'T'))
  if (isNaN(d)) return ts
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

onMounted(() => {
  if (!auth.isAuthed) return
  fetch()
  if (health.archiveDays === null) health.check() // the days left need it
})
</script>

<template>
  <q-page class="q-pa-md">
    <div class="row items-center q-col-gutter-sm q-mb-md">
      <div class="col-auto text-h6">Archived</div>
      <div class="col-auto text-caption text-grey">
        <span v-if="health.archiveDays > 0">Deleted {{ health.archiveDays }} days after they were archived, unless restored.</span>
        <span v-else-if="health.archiveDays === 0">Kept until restored or deleted by hand.</span>
      </div>
      <q-space />
      <q-btn flat dense round icon="refresh" aria-label="Reload" :loading="loading" @click="fetch" />
    </div>

    <q-banner v-if="error" dense class="bg-orange-1 text-orange-9 rounded-borders q-mb-sm">
      <template #avatar><q-icon name="warning" /></template>
      {{ error }}
    </q-banner>

    <q-table
      flat
      bordered
      row-key="id"
      :rows="rows"
      :columns="columns"
      :loading="loading"
      :pagination="{ page: 1, rowsPerPage: 0 }"
      :rows-per-page-options="[0]"
      wrap-cells
    >
      <template #top>
        <div class="text-subtitle2">{{ total }} archived</div>
      </template>

      <template #body-cell-left="props">
        <q-td :props="props" data-test="days-left">{{ leftLabel(props.row) }}</q-td>
      </template>

      <template #body-cell-content="props">
        <q-td :props="props" style="max-width: 420px; min-width: 240px">
          <div class="arc-content">{{ props.row.content }}</div>
        </q-td>
      </template>

      <template #body-cell-reason="props">
        <q-td :props="props" style="max-width: 360px; min-width: 200px">
          <div v-if="props.row.review" class="text-caption">
            <q-badge outline :color="props.row.review.verdict === 'reject' ? 'negative' : 'grey-7'"
                     :label="props.row.review.verdict" />
            <span v-if="props.row.review.rule != null" class="q-ml-xs">rule {{ props.row.review.rule }}</span>
            <span v-if="props.row.review.reason" class="q-ml-xs">· {{ props.row.review.reason }}</span>
          </div>
          <div v-if="props.row.superseded_by" class="text-caption text-grey q-mt-xs">
            merged into #{{ props.row.superseded_by }}
          </div>
        </q-td>
      </template>

      <template #body-cell-actions="props">
        <q-td :props="props">
          <q-btn outline dense no-caps size="sm" color="primary" icon="unarchive" label="Restore"
                 :loading="restoring.has(props.row.id)" @click="restore(props.row)" />
        </q-td>
      </template>

      <template #no-data>
        <div class="full-width row flex-center text-grey q-py-md">
          Nothing archived.
        </div>
      </template>
    </q-table>
  </q-page>
</template>

<style scoped>
.arc-content {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: normal;
  word-break: break-word;
}
</style>
