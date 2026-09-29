<script setup>
// Paginated memories table (server-side) + create / edit / delete.
//
// Two ways to feed it:
//   • store mode (default): rows, total, order, loading and paging come from
//     the `memories` store. We bind `:pagination` from a computed view of the
//     store and react to q-table's `@request` — no local copy of page/sort
//     state. Only Date / Agent / Project / Type are sortable (→ memories.order).
//   • given rows: a parent passes `rows` (+ `total`, `loading`). The table
//     shows them as they are, with no paging or sorting, and emits `changed`
//     after a create or delete so the parent can fetch again. The Flagged view
//     uses this so both views show the same cards.
//
// Every row shows its review status; a row with a review shows the verdict,
// the rule, the reason and, for a rewrite, the suggested text and tags. The
// `supersedes` / `superseded by` links open the other memory in the detail
// dialog.
//
// Mutations:
//   • edit   → PATCH, then OPTIMISTICALLY patch the row in place (no refetch —
//              an edit that no longer matches the filter stays until refresh).
//   • create → POST, then fetch again.
//   • delete → DELETE (confirm), then fetch again.
//   • review → POST /admin/review/{id}, then reload that one row.
import { computed, ref } from 'vue'
import { useQuasar } from 'quasar'
import { useMemoriesStore, TYPE_COLOR } from '@/stores/memories'
import { api } from '@/api/client'
import MemoryEditDialog from './MemoryEditDialog.vue'

const props = defineProps({
  // Rows to show instead of the store's. null → store mode.
  rows: { type: Array, default: null },
  total: { type: Number, default: null },
  loading: { type: Boolean, default: false },
})
const emit = defineEmits(['changed'])

const $q = useQuasar()
const memories = useMemoriesStore()

const storeMode = computed(() => props.rows === null)
const displayRows = computed(() => (storeMode.value ? memories.results : props.rows))
const displayTotal = computed(() =>
  storeMode.value ? memories.total : (props.total ?? props.rows.length),
)
const isLoading = computed(() => (storeMode.value ? memories.loading : props.loading))

// The semantic and hybrid modes come back ranked, with a score and no pages.
const showScore = computed(() => displayRows.value.some((r) => r.score != null))

const columns = computed(() => {
  const sortable = storeMode.value && !memories.ranked
  const cols = [
    { name: 'id', label: 'ID', field: 'id', align: 'left', style: 'width: 60px' },
    { name: 'date', label: 'Date', field: 'timestamp', align: 'left', sortable, format: (v) => fmtDate(v) },
    { name: 'status', label: 'Status', field: 'review_status', align: 'left', style: 'width: 90px' },
  ]
  if (showScore.value) {
    cols.push({ name: 'score', label: 'Score', field: 'score', align: 'right', style: 'width: 70px', format: (v) => fmtScore(v) })
  }
  cols.push(
    { name: 'agent', label: 'Agent', field: 'agent', align: 'left', sortable },
    { name: 'project', label: 'Project', field: (r) => r.project || '—', align: 'left', sortable },
    { name: 'type', label: 'Type', field: (r) => r.type || '—', align: 'left', sortable },
    { name: 'content', label: 'Content', field: 'content', align: 'left' },
    { name: 'tags', label: 'Tags', field: 'tags', align: 'left' },
    { name: 'actions', label: '', field: 'actions', align: 'right', style: 'width: 90px' },
  )
  return cols
})

// order is "<field>_<asc|desc>"; split it for q-table's pagination shape.
function splitOrder(order) {
  const i = String(order || 'date_desc').lastIndexOf('_')
  return [order.slice(0, i), order.slice(i + 1)]
}

// q-table pagination shape, derived read-only from the store. With given rows
// (or a ranked search) there are no pages: rowsPerPage 0 shows every row.
const pagination = computed(() => {
  if (!storeMode.value) {
    return { page: 1, rowsPerPage: 0, rowsNumber: displayRows.value.length }
  }
  const [field, dir] = splitOrder(memories.order)
  return {
    sortBy: field,
    descending: dir === 'desc',
    page: memories.page,
    rowsPerPage: memories.ranked ? 0 : memories.limit,
    rowsNumber: memories.ranked ? memories.results.length : memories.total,
  }
})

/** q-table server-side driver: user paged or toggled a sortable column
 *  (Date / Agent / Project / Type). */
function onRequest(props) {
  if (!storeMode.value) return
  const { page, sortBy, descending } = props.pagination
  const nextOrder = `${sortBy || 'date'}_${descending ? 'desc' : 'asc'}`
  let targetPage = page
  if (nextOrder !== memories.order) {
    memories.order = nextOrder
    targetPage = 1 // new sort → back to first page
  }
  memories.setPage(targetPage)
  memories.fetch()
}

/** Fetch again after a change: the store in store mode, else the parent. */
function refresh() {
  if (storeMode.value) memories.fetch()
  else emit('changed')
}

/** Merge fields onto one loaded row, whichever list it lives in. */
function patchRow(id, fields) {
  const row = displayRows.value.find((r) => r.id === id)
  if (row) Object.assign(row, fields)
}

// ---- type / status / review ---------------------------------------------
function typeColor(type) {
  return TYPE_COLOR[type] || 'grey-6'
}

const STATUS_COLOR = { unverified: 'grey-6', verified: 'positive', flagged: 'warning' }
function statusColor(status) {
  return STATUS_COLOR[status] || 'grey-6'
}

const VERDICT_COLOR = { approve: 'positive', reject: 'negative', rewrite: 'warning' }
function verdictColor(verdict) {
  return VERDICT_COLOR[verdict] || 'grey-7'
}

function fmtScore(v) {
  if (v == null) return ''
  return Number(v).toFixed(3)
}

const reviewing = ref(new Set()) // ids with a review call in flight

/** Ask the model again about one memory, then reload that row. */
async function reviewAgain(row) {
  reviewing.value = new Set([...reviewing.value, row.id])
  try {
    await api.reviewMemory(row.id)
    const fresh = await api.getMemory(row.id)
    patchRow(row.id, {
      review: fresh.review,
      review_status: fresh.review_status,
      supersedes: fresh.supersedes,
      superseded_by: fresh.superseded_by,
    })
    $q.notify({ type: 'positive', message: `Memory #${row.id} reviewed: ${fresh.review?.verdict ?? 'no verdict'}` })
  } catch (err) {
    $q.notify({ type: 'negative', message: errorText(err, 'Review failed') })
  } finally {
    const next = new Set(reviewing.value)
    next.delete(row.id)
    reviewing.value = next
  }
}

/** The server's `detail` when it sent one, else the error's own message. */
function errorText(err, fallback) {
  const detail = err?.data?.detail
  if (typeof detail === 'string') return detail
  return err?.message || fallback
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

// ---- create / edit / delete ---------------------------------------------
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

/** Open another memory (a supersedes link) in the detail dialog. It may not
 *  be on this page, so it is fetched by id. */
async function openById(id) {
  try {
    openEdit(await api.getMemory(id))
  } catch (err) {
    $q.notify({ type: 'negative', message: errorText(err, `Memory #${id} not found`) })
  }
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
      patchRow(payload.id, {
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
      refresh()
      $q.notify({ type: 'positive', message: `Memory #${id} created` })
    }
  } catch (err) {
    $q.notify({ type: 'negative', message: errorText(err, 'Save failed') })
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
      if (storeMode.value) memories.removeRow(row.id)  // drop it from the list immediately
      $q.notify({ type: 'positive', message: `Memory #${row.id} deleted` })
      refresh()                                        // reconcile/backfill in the background
    } catch (err) {
      $q.notify({ type: 'negative', message: errorText(err, 'Delete failed') })
    }
  })
}
</script>

<template>
  <!-- The server refused the query (for example semantic search with no model). -->
  <q-banner v-if="storeMode && memories.error" dense class="bg-orange-1 text-orange-9 rounded-borders q-mb-sm">
    <template #avatar><q-icon name="warning" /></template>
    {{ errorText(memories.error, 'The request failed') }}
  </q-banner>

  <q-table
    flat
    bordered
    row-key="id"
    :rows="displayRows"
    :columns="columns"
    :loading="isLoading"
    :pagination="pagination"
    :rows-per-page-options="[0, 100]"
    binary-state-sort
    wrap-cells
    @request="onRequest"
    @row-click="(evt, row) => openEdit(row)"
  >
    <!-- Toolbar: count + New memory -->
    <template #top>
      <div class="text-subtitle2">
        <slot name="title">
          {{ displayTotal }} memories<span v-if="memories.q"> matching “{{ memories.q }}”</span>
          <span v-if="memories.q && memories.mode !== 'keyword'" class="text-grey"> ({{ memories.mode }})</span>
        </slot>
      </div>
      <q-space />
      <q-btn color="primary" icon="add" no-caps label="New memory" @click="openCreate" />
    </template>

    <!-- Type: a filled red badge for a constraint (a hard rule), an outline
         badge for the other types, a dash for none -->
    <template #body-cell-type="props">
      <q-td :props="props">
        <q-badge v-if="props.row.type" :color="typeColor(props.row.type)" :label="props.row.type"
                 :outline="props.row.type !== 'constraint'" class="mem-type" />
        <span v-else>—</span>
      </q-td>
    </template>

    <!-- Status: unverified (grey), verified (green), flagged (amber) -->
    <template #body-cell-status="props">
      <q-td :props="props">
        <q-badge :color="statusColor(props.row.review_status)" :label="props.row.review_status || 'unverified'"
                 class="mem-status" />
      </q-td>
    </template>

    <!-- Content: snippet-highlighted when searching, else clamped preview;
         then the links and the review, when present -->
    <template #body-cell-content="props">
      <q-td :props="props" style="max-width: 460px; min-width: 260px">
        <div
          v-if="props.row.snippet"
          class="mem-snippet"
          v-html="renderSnippet(props.row.snippet)"
        />
        <div v-else class="mem-content">{{ props.row.content }}</div>

        <!-- supersedes / superseded by: open the other memory -->
        <div v-if="props.row.supersedes || props.row.superseded_by" class="text-caption q-mt-xs mem-links">
          <a v-if="props.row.supersedes" href="#" class="mem-link" @click.prevent.stop="openById(props.row.supersedes)">
            <q-icon name="history" size="xs" /> supersedes #{{ props.row.supersedes }}
          </a>
          <a v-if="props.row.superseded_by" href="#" class="mem-link" @click.prevent.stop="openById(props.row.superseded_by)">
            <q-icon name="update" size="xs" /> superseded by #{{ props.row.superseded_by }}
          </a>
        </div>

        <!-- the review: verdict, rule, reason; a rewrite adds the suggestion -->
        <div v-if="props.row.review" class="mem-review q-mt-xs" @click.stop>
          <div class="text-caption">
            <q-badge outline :color="verdictColor(props.row.review.verdict)" :label="props.row.review.verdict" />
            <span v-if="props.row.review.rule != null" class="q-ml-xs">rule {{ props.row.review.rule }}</span>
            <span v-if="props.row.review.reason" class="q-ml-xs">· {{ props.row.review.reason }}</span>
            <a v-if="props.row.review.duplicate_of" href="#" class="mem-link q-ml-xs"
               @click.prevent.stop="openById(props.row.review.duplicate_of)">
              duplicate of #{{ props.row.review.duplicate_of }}
            </a>
          </div>
          <template v-if="props.row.review.verdict === 'rewrite'">
            <blockquote v-if="props.row.review.rewrite" class="mem-rewrite">{{ props.row.review.rewrite }}</blockquote>
            <div v-if="props.row.review.tags?.length" class="q-mt-xs">
              <span class="text-caption text-grey q-mr-xs">suggested tags:</span>
              <q-chip
                v-for="t in props.row.review.tags"
                :key="t"
                dense
                size="sm"
                outline
                color="warning"
                class="q-ma-none q-mr-xs"
              >
                {{ t }}
              </q-chip>
            </div>
          </template>
          <q-btn flat dense no-caps size="sm" icon="refresh" label="Review again"
                 class="q-mt-xs q-px-xs" color="primary"
                 :loading="reviewing.has(props.row.id)"
                 @click.stop="reviewAgain(props.row)" />
        </div>
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
        <q-btn flat dense round icon="delete" size="sm" color="red-4" aria-label="Delete"
               @click="confirmDelete(props.row)" />
      </q-td>
    </template>

    <template #no-data>
      <div class="full-width row flex-center text-grey q-py-md">
        <slot name="empty">No memories.</slot>
      </div>
    </template>
  </q-table>

  <MemoryEditDialog v-model="dialogOpen" :memory="editing" @submit="onSubmit" @open="openById" />
</template>

<style scoped>
/* Whole row opens the edit modal — signal it. Action buttons stop propagation. */
:deep(.q-table tbody tr) {
  cursor: pointer;
}
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
.mem-status,
.mem-type {
  text-transform: lowercase;
}
.mem-links .mem-link + .mem-link {
  margin-left: 10px;
}
.mem-link {
  color: var(--q-primary);
  text-decoration: none;
}
.mem-link:hover {
  text-decoration: underline;
}
.mem-review {
  cursor: default;
}
.mem-rewrite {
  margin: 4px 0 0;
  padding: 4px 10px;
  border-left: 3px solid var(--q-warning);
  font-size: 0.9rem;
  white-space: pre-wrap;
  word-break: break-word;
  opacity: 0.9;
}
</style>
