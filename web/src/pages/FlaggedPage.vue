<script setup>
// Flagged view: the memories the review rejected or wants rewritten, newest
// review first, from GET /memories/flagged. The same table (and cards) as the
// memories view, fed with its own rows. A verdict filter narrows the list;
// "Catch up" asks the server to review the unverified memories
// (POST /admin/review) and shows what it scheduled.
import { onMounted, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useAuthStore } from '@/stores/auth'
import { useTagsStore } from '@/stores/tags'
import { useHealthStore } from '@/stores/health'
import { api } from '@/api/client'
import MemoriesTable from '@/components/MemoriesTable.vue'

const $q = useQuasar()
const auth = useAuthStore()
const tags = useTagsStore()
const health = useHealthStore()

const VERDICT_OPTIONS = [
  { label: 'all verdicts', value: '' },
  { label: 'reject', value: 'reject' },
  { label: 'rewrite', value: 'rewrite' },
]

const verdict = ref('')
const rows = ref([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

// What the last catch-up call said: "scheduled 12", "running", or ''.
const catchUp = ref('')
const catchingUp = ref(false)

async function fetch() {
  loading.value = true
  error.value = ''
  try {
    const { items, total: t } = await api.flaggedMemories({
      verdict: verdict.value || undefined,
      limit: 0,
    })
    rows.value = items
    total.value = t
  } catch (err) {
    error.value = detail(err, 'Could not load the flagged memories')
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

/** Review the unverified memories, oldest first, in the background. */
async function runCatchUp() {
  catchingUp.value = true
  try {
    const r = await api.reviewCatchUp()
    health.check() // show the progress bar now, not at the next slow poll
    if (r.running) {
      catchUp.value = 'running'
      $q.notify({ type: 'info', message: 'A catch-up is already running.' })
    } else {
      catchUp.value = `scheduled ${r.scheduled}`
      $q.notify({
        type: r.scheduled ? 'positive' : 'info',
        message: r.scheduled
          ? `Scheduled ${r.scheduled} unverified memories for review.`
          : 'Nothing to review: every memory has a verdict.',
      })
    }
  } catch (err) {
    catchUp.value = ''
    $q.notify({ type: 'negative', message: detail(err, 'Catch-up failed') })
  } finally {
    catchingUp.value = false
  }
}

function detail(err, fallback) {
  const d = err?.data?.detail
  return typeof d === 'string' ? d : (err?.message || fallback)
}

onMounted(() => {
  if (!auth.isAuthed) return
  fetch()
  if (!tags.list.length) tags.fetch() // the edit dialog's tag picker
})

// A catch-up just ended: reload the list once to show the new verdicts.
watch(() => health.ended, () => { if (auth.isAuthed) fetch() })
</script>

<template>
  <q-page class="q-pa-md">
    <div class="row items-center q-col-gutter-sm q-mb-md">
      <div class="col-auto text-h6">Flagged</div>
      <div class="col-auto">
        <q-select
          v-model="verdict"
          :options="VERDICT_OPTIONS"
          dense
          outlined
          emit-value
          map-options
          style="min-width: 150px"
          aria-label="Verdict"
          @update:model-value="fetch"
        />
      </div>
      <q-space />
      <div class="col-auto row items-center q-gutter-sm">
        <span v-if="catchUp" class="text-caption text-grey" data-test="catch-up-result">{{ catchUp }}</span>
        <q-btn outline no-caps color="primary" icon="playlist_add_check" label="Catch up"
               :loading="catchingUp" @click="runCatchUp">
          <q-tooltip>Review the unverified memories, oldest first.</q-tooltip>
        </q-btn>
        <q-btn flat dense round icon="refresh" aria-label="Reload" :loading="loading" @click="fetch" />
      </div>
    </div>

    <q-banner v-if="error" dense class="bg-orange-1 text-orange-9 rounded-borders q-mb-sm">
      <template #avatar><q-icon name="warning" /></template>
      {{ error }}
    </q-banner>

    <MemoriesTable :rows="rows" :total="total" :loading="loading" @changed="fetch">
      <template #title>
        {{ total }} flagged<span v-if="verdict"> ({{ verdict }})</span>
      </template>
      <template #empty>Nothing flagged. The review approved everything it has read.</template>
    </MemoriesTable>
  </q-page>
</template>
