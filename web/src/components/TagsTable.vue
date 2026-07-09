<script setup>
// D7 — Tags management table. A q-table over `tags.list` ({ name, count,
// description }) with alphabetical-by-name default sort (name column sortable),
// inline rename / re-describe (PATCH /tags/{name} — a rename onto an existing
// name merges, which we surface), and delete (DELETE /tags/{name}). Every
// mutation re-runs tags.fetch(). Clicking a row emits `open` so TagsPage can
// pop the D8 detail modal.
import { ref, computed } from 'vue'
import { useQuasar } from 'quasar'
import { storeToRefs } from 'pinia'
import { useTagsStore } from '@/stores/tags'
import { api } from '@/api/client'

const emit = defineEmits(['open'])

const $q = useQuasar()
const tags = useTagsStore()
const { list, loading } = storeToRefs(tags)

// Columns. Name is sortable (asc/desc); count is sortable too for convenience.
// Default sort is alphabetical by name — set via the pagination model below.
const columns = [
  {
    name: 'name',
    label: 'Name',
    field: 'name',
    align: 'left',
    sortable: true,
    sort: (a, b) => String(a).localeCompare(String(b)),
  },
  { name: 'count', label: 'Count', field: 'count', align: 'right', sortable: true },
  { name: 'description', label: 'Description', field: 'description', align: 'left', sortable: false },
  { name: 'actions', label: '', field: 'actions', align: 'right', sortable: false },
]

// rowsPerPage 0 = show all rows (no client pagination); default alpha sort asc.
const pagination = ref({ sortBy: 'name', descending: false, rowsPerPage: 0 })

// ---- Edit (rename / re-describe) dialog -----------------------------------
const editOpen = ref(false)
const editSaving = ref(false)
const editTarget = ref(null) // the original row being edited
const editName = ref('')
const editDescription = ref('')

function startEdit(row) {
  editTarget.value = row
  editName.value = row.name
  editDescription.value = row.description ?? ''
  editOpen.value = true
}

// A rename collides (→ merge) when the new name matches a *different* existing
// tag, case-insensitively (mirrors the server / mock semantics).
const collidesInto = computed(() => {
  if (!editTarget.value) return null
  const next = editName.value.trim()
  if (!next || next.toLowerCase() === editTarget.value.name.toLowerCase()) return null
  const hit = list.value.find(
    (t) => t.name.toLowerCase() === next.toLowerCase() && t.name !== editTarget.value.name,
  )
  return hit ? hit.name : null
})

async function saveEdit() {
  const original = editTarget.value
  if (!original) return
  const nextName = editName.value.trim()
  const nextDesc = editDescription.value
  const patch = {}
  if (nextName && nextName !== original.name) patch.name = nextName
  if (nextDesc !== (original.description ?? '')) patch.description = nextDesc
  if (!patch.name && patch.description === undefined) {
    editOpen.value = false
    return
  }
  const merged = !!(patch.name && collidesInto.value)
  editSaving.value = true
  try {
    const res = await api.patchTag(original.name, patch)
    editOpen.value = false
    if (merged) {
      $q.notify({
        type: 'warning',
        message: `Merged “${original.name}” into “${res.name}” (now ${res.count} memories).`,
      })
    } else if (patch.name) {
      $q.notify({ type: 'positive', message: `Renamed to “${res.name}”.` })
    } else {
      $q.notify({ type: 'positive', message: `Updated “${res.name}”.` })
    }
    await tags.fetch()
  } catch (err) {
    $q.notify({ type: 'negative', message: err?.data?.detail || err?.message || 'Update failed.' })
  } finally {
    editSaving.value = false
  }
}

// ---- Delete ----------------------------------------------------------------
function confirmDelete(row) {
  $q.dialog({
    title: 'Delete tag',
    message: `Delete “${row.name}” and remove it from ${row.count} memor${row.count === 1 ? 'y' : 'ies'}? This can’t be undone.`,
    cancel: true,
    persistent: true,
    ok: { label: 'Delete', color: 'negative' },
  }).onOk(async () => {
    try {
      const res = await api.deleteTag(row.name)
      $q.notify({
        type: 'positive',
        message: `Deleted “${res.removed}” (${res.memories_affected} memories affected).`,
      })
      await tags.fetch()
    } catch (err) {
      $q.notify({ type: 'negative', message: err?.data?.detail || err?.message || 'Delete failed.' })
    }
  })
}
</script>

<template>
  <q-table
    flat
    bordered
    row-key="name"
    :rows="list"
    :columns="columns"
    :loading="loading"
    v-model:pagination="pagination"
    hide-bottom
    :rows-per-page-options="[0]"
    @row-click="(evt, row) => emit('open', row)"
  >
    <template #body-cell-count="props">
      <q-td :props="props">
        <q-badge color="primary">{{ props.value }}</q-badge>
      </q-td>
    </template>

    <template #body-cell-description="props">
      <q-td :props="props" class="text-grey-8">
        {{ props.value || '—' }}
      </q-td>
    </template>

    <template #body-cell-actions="props">
      <q-td :props="props" @click.stop>
        <q-btn dense flat round icon="edit" size="sm" @click.stop="startEdit(props.row)">
          <q-tooltip>Rename / re-describe</q-tooltip>
        </q-btn>
        <q-btn dense flat round icon="delete" size="sm" color="negative" @click.stop="confirmDelete(props.row)">
          <q-tooltip>Delete tag</q-tooltip>
        </q-btn>
      </q-td>
    </template>

    <template #no-data>
      <div class="full-width row flex-center text-grey q-py-md">No tags.</div>
    </template>
  </q-table>

  <!-- Rename / re-describe dialog -->
  <q-dialog v-model="editOpen">
    <q-card style="min-width: 360px">
      <q-card-section>
        <div class="text-h6">Edit tag</div>
      </q-card-section>
      <q-card-section class="q-gutter-md">
        <q-input v-model="editName" label="Name" autofocus dense outlined @keyup.enter="saveEdit" />
        <q-input v-model="editDescription" label="Description" dense outlined type="textarea" autogrow />
        <q-banner v-if="collidesInto" dense class="bg-orange-1 text-orange-9 rounded-borders">
          <template #avatar><q-icon name="merge" color="orange-9" /></template>
          A tag named “{{ collidesInto }}” already exists — saving will
          <b>merge</b> “{{ editTarget?.name }}” into it.
        </q-banner>
      </q-card-section>
      <q-card-actions align="right">
        <q-btn flat label="Cancel" v-close-popup :disable="editSaving" />
        <q-btn
          color="primary"
          :label="collidesInto ? 'Merge' : 'Save'"
          :loading="editSaving"
          @click="saveEdit"
        />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>
