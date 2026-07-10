<script setup>
// D8 — Tag detail modal. Opens on a TagsTable row click. Lists the memories
// carrying the tag (GET /memories?tag=<name>), lets you detach the tag from
// selected memories or from ALL of them (POST /tags/{name}/detach — omit/empty
// ids = all), and consolidate tags via a merge dialog (POST /tags/merge:
// N sources → 1 target, target description editable). Every mutation refreshes
// both this list and the shared tags store (tags.fetch()).
import { ref, computed, watch } from 'vue'
import { useQuasar } from 'quasar'
import { storeToRefs } from 'pinia'
import { useTagsStore } from '@/stores/tags'
import { api } from '@/api/client'
import MemoryEditDialog from '@/components/MemoryEditDialog.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  tag: { type: Object, default: null }, // { name, count, description }
})
const emit = defineEmits(['update:modelValue'])

const $q = useQuasar()
const tags = useTagsStore()
const { list } = storeToRefs(tags)

const open = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const tagName = computed(() => props.tag?.name ?? '')

// ---- Memories carrying this tag -------------------------------------------
const memories = ref([]) // MemoryOut[]
const memLoading = ref(false)
const selected = ref([]) // selected memory ids
const busy = ref(false)

async function loadMemories() {
  if (!tagName.value) return
  memLoading.value = true
  selected.value = []
  try {
    const { items } = await api.listMemories({ tag: [tagName.value], limit: 100 })
    memories.value = items
  } catch (err) {
    memories.value = []
    $q.notify({ type: 'negative', message: err?.data?.detail || err?.message || 'Failed to load memories.' })
  } finally {
    memLoading.value = false
  }
}

// (Re)load whenever the dialog opens on a tag.
watch(
  () => [props.modelValue, tagName.value],
  ([isOpen]) => {
    if (isOpen && tagName.value) loadMemories()
  },
  { immediate: true },
)

const allSelected = computed({
  get: () => memories.value.length > 0 && selected.value.length === memories.value.length,
  set: (v) => {
    selected.value = v ? memories.value.map((m) => m.id) : []
  },
})

// ---- Edit a memory (opens the same dialog as the memories table) ----------
const editOpen = ref(false)
const editing = ref(null)

function openEdit(m) {
  editing.value = m
  editOpen.value = true
}

async function onEditSubmit(payload) {
  try {
    await api.patchMemory(payload.id, {
      content: payload.content,
      project: payload.project,
      type: payload.type,
      set_tags: payload.tagNames.map((name) => ({ name })),
    })
    $q.notify({ type: 'positive', message: `Memory #${payload.id} updated` })
    // Re-load: an edit may have removed this tag from the memory.
    await Promise.all([loadMemories(), tags.fetch()])
  } catch (err) {
    $q.notify({ type: 'negative', message: err?.data?.detail || err?.message || 'Save failed.' })
  }
}

// ---- Detach ----------------------------------------------------------------
async function detach(ids) {
  // ids omitted / empty ⇒ detach from ALL memories (per contract).
  busy.value = true
  try {
    const res = await api.detachTag(tagName.value, ids && ids.length ? ids : undefined)
    $q.notify({ type: 'positive', message: `Detached “${tagName.value}” from ${res.detached} memor${res.detached === 1 ? 'y' : 'ies'}.` })
    await Promise.all([loadMemories(), tags.fetch()])
  } catch (err) {
    $q.notify({ type: 'negative', message: err?.data?.detail || err?.message || 'Detach failed.' })
  } finally {
    busy.value = false
  }
}

function detachSelected() {
  if (!selected.value.length) return
  detach(selected.value.slice())
}

function detachAll() {
  $q.dialog({
    title: 'Detach from all',
    message: `Remove “${tagName.value}” from every memory that carries it? The tag itself stays.`,
    cancel: true,
    persistent: true,
    ok: { label: 'Detach all', color: 'negative' },
  }).onOk(() => detach(undefined))
}

// ---- Merge (consolidate) dialog -------------------------------------------
const mergeOpen = ref(false)
const mergeSaving = ref(false)
const mergeSources = ref([]) // tag names
const mergeTarget = ref('')
const mergeDescription = ref('')

// Options are every tag name (from the shared store).
const tagOptions = computed(() => list.value.map((t) => t.name))
// Sources can't include the chosen target.
const sourceOptions = computed(() => tagOptions.value.filter((n) => n !== mergeTarget.value))

function openMerge() {
  // Sensible defaults: the current tag is the target, keep its description.
  mergeTarget.value = tagName.value
  mergeDescription.value = props.tag?.description ?? ''
  mergeSources.value = []
  mergeOpen.value = true
}

// Keep description in step with whichever existing tag is the target.
watch(mergeTarget, (name) => {
  const hit = list.value.find((t) => t.name === name)
  mergeDescription.value = hit ? (hit.description ?? '') : mergeDescription.value
  // Drop the target from sources if it slipped in.
  mergeSources.value = mergeSources.value.filter((n) => n !== name)
})

const canMerge = computed(() => mergeTarget.value.trim() && mergeSources.value.length > 0)

async function doMerge() {
  if (!canMerge.value) return
  mergeSaving.value = true
  try {
    const res = await api.mergeTags({
      sources: mergeSources.value.slice(),
      target: mergeTarget.value.trim(),
      description: mergeDescription.value,
    })
    mergeOpen.value = false
    $q.notify({
      type: 'positive',
      message: `Merged ${res.removed.length} tag${res.removed.length === 1 ? '' : 's'} into “${res.target}” (${res.memories_affected} memories affected).`,
    })
    await tags.fetch()
    // If the tag we're viewing was consumed as a source, close the modal;
    // otherwise refresh its memory list (target may have gained memories).
    if (res.removed.includes(tagName.value)) open.value = false
    else await loadMemories()
  } catch (err) {
    $q.notify({ type: 'negative', message: err?.data?.detail || err?.message || 'Merge failed.' })
  } finally {
    mergeSaving.value = false
  }
}
</script>

<template>
  <q-dialog v-model="open">
    <q-card style="min-width: 420px; max-width: 720px; width: 90vw">
      <q-card-section class="row items-center q-pb-none">
        <div>
          <div class="text-h6">{{ tagName }}</div>
          <div class="text-caption text-grey">{{ tag?.description || '—' }}</div>
        </div>
        <q-space />
        <q-btn dense flat round icon="merge" @click="openMerge">
          <q-tooltip>Consolidate / merge tags</q-tooltip>
        </q-btn>
        <q-btn dense flat round icon="close" v-close-popup />
      </q-card-section>

      <q-card-section>
        <div class="row items-center justify-between q-mb-sm">
          <div class="text-subtitle2">
            {{ memories.length }} memor{{ memories.length === 1 ? 'y' : 'ies' }}
            <span v-if="selected.length" class="text-grey"> · {{ selected.length }} selected</span>
          </div>
          <q-checkbox v-model="allSelected" label="Select all" dense :disable="!memories.length" />
        </div>

        <q-list bordered separator style="max-height: 60vh; overflow: auto">
          <q-item v-for="m in memories" :key="m.id">
            <q-item-section side top>
              <q-checkbox v-model="selected" :val="m.id" />
            </q-item-section>
            <q-item-section class="cursor-pointer" @click="openEdit(m)">
              <q-item-label class="mem-full-text">{{ m.content }}</q-item-label>
              <q-item-label caption>
                #{{ m.id }} · {{ m.agent }} · {{ m.project || '—' }} · {{ m.type || '—' }}
                <span class="text-primary"> · click to edit</span>
              </q-item-label>
            </q-item-section>
          </q-item>
          <q-item v-if="!memLoading && !memories.length">
            <q-item-section class="text-grey">No memories carry this tag.</q-item-section>
          </q-item>
        </q-list>
        <q-inner-loading :showing="memLoading" />
      </q-card-section>

      <q-card-actions align="right">
        <q-btn
          flat
          color="negative"
          label="Detach selected"
          :disable="!selected.length || busy"
          @click="detachSelected"
        />
        <q-btn
          flat
          color="negative"
          label="Detach all"
          :disable="!memories.length || busy"
          @click="detachAll"
        />
        <q-space />
        <q-btn flat label="Close" v-close-popup />
      </q-card-actions>
    </q-card>
  </q-dialog>

  <!-- Merge / consolidate dialog -->
  <q-dialog v-model="mergeOpen">
    <q-card style="min-width: 380px">
      <q-card-section>
        <div class="text-h6">Consolidate tags</div>
        <div class="text-caption text-grey">
          Reassign every memory from the source tags to the target, then delete the sources.
        </div>
      </q-card-section>
      <q-card-section class="q-gutter-md">
        <q-select
          v-model="mergeTarget"
          :options="tagOptions"
          label="Target tag"
          dense
          outlined
          use-input
          hide-selected
          fill-input
          new-value-mode="add-unique"
          input-debounce="0"
          hint="Kept (created if new). Sources merge into this."
        />
        <q-select
          v-model="mergeSources"
          :options="sourceOptions"
          label="Source tags (merged in & deleted)"
          multiple
          use-chips
          dense
          outlined
        />
        <q-input
          v-model="mergeDescription"
          label="Target description"
          dense
          outlined
          type="textarea"
          autogrow
          hint="Editable — applied to the target tag."
        />
      </q-card-section>
      <q-card-actions align="right">
        <q-btn flat label="Cancel" v-close-popup :disable="mergeSaving" />
        <q-btn color="primary" label="Merge" :loading="mergeSaving" :disable="!canMerge" @click="doMerge" />
      </q-card-actions>
    </q-card>
  </q-dialog>

  <!-- Edit a clicked memory — same dialog the memories table uses -->
  <MemoryEditDialog v-model="editOpen" :memory="editing" @submit="onEditSubmit" />
</template>

<style scoped>
/* Full memory text, no clamp/truncation — wrap naturally, preserve line breaks. */
.mem-full-text {
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
