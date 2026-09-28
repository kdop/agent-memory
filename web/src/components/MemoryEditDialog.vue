<script setup>
// Create / edit form dialog. Pure form: it gathers values and emits
// `submit`; the table (MemoriesTable) owns the API orchestration so the
// "what to do after a mutation" policy lives in one place (optimistic edit vs
// refetch on create). `memory` prop present → edit mode; null → create mode.
// Tag editing uses the structured { name, description? } shape on save.
// In edit mode the header also shows the read-only review facts: the status,
// the verdict and the supersedes links, which emit `open` with the other id.
import { reactive, computed, watch } from 'vue'
import { useTagsStore } from '@/stores/tags'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  memory: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'submit', 'open'])

const tags = useTagsStore()
const TYPES = ['decision', 'code', 'lesson', 'note']

const isEdit = computed(() => !!props.memory)

const STATUS_COLOR = { unverified: 'grey-6', verified: 'positive', flagged: 'warning' }
const statusColor = computed(() => STATUS_COLOR[props.memory?.review_status] || 'grey-6')

/** Open another memory in this dialog (a supersedes link). */
function openOther(id) {
  emit('open', id)
}

const form = reactive({
  content: '',
  agent: '',
  project: '',
  type: null,
  tagNames: [],
})

function reset() {
  const m = props.memory
  form.content = m?.content ?? ''
  form.agent = m?.agent ?? ''
  form.project = m?.project ?? ''
  form.type = m?.type ?? null
  form.tagNames = m ? [...m.tags] : []
}

// Repopulate the form each time the dialog opens, and when a supersedes link
// swaps the memory while it is already open.
watch(
  () => [props.modelValue, props.memory],
  ([open]) => { if (open) reset() },
)

// Existing tag names for the picker; users may also type new ones (add-unique).
const tagOptions = computed(() => tags.list.map((t) => t.name))

const canSave = computed(() => form.content.trim().length > 0)

function save() {
  if (!canSave.value) return
  emit('submit', {
    id: props.memory?.id,
    isEdit: isEdit.value,
    content: form.content.trim(),
    agent: form.agent.trim(),
    project: form.project ?? '',
    type: form.type ?? '',
    tagNames: [...form.tagNames],
  })
  emit('update:modelValue', false)
}

function close() {
  emit('update:modelValue', false)
}
</script>

<template>
  <q-dialog
    :model-value="modelValue"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <q-card style="min-width: 480px; max-width: 90vw">
      <q-card-section>
        <div class="row items-center q-gutter-sm">
          <div class="text-h6">{{ isEdit ? `Edit memory #${memory.id}` : 'New memory' }}</div>
          <q-badge v-if="isEdit" :color="statusColor" :label="memory.review_status || 'unverified'" />
        </div>
        <!-- Read-only review facts: what the model said and what this replaces. -->
        <div v-if="isEdit && (memory.review || memory.supersedes || memory.superseded_by)"
             class="text-caption text-grey q-mt-xs">
          <span v-if="memory.review">
            {{ memory.review.verdict }}<template v-if="memory.review.rule != null">, rule {{ memory.review.rule }}</template>:
            {{ memory.review.reason }}
          </span>
          <div v-if="memory.supersedes || memory.superseded_by">
            <a v-if="memory.supersedes" href="#" class="text-primary"
               @click.prevent="openOther(memory.supersedes)">supersedes #{{ memory.supersedes }}</a>
            <span v-if="memory.supersedes && memory.superseded_by"> · </span>
            <a v-if="memory.superseded_by" href="#" class="text-primary"
               @click.prevent="openOther(memory.superseded_by)">superseded by #{{ memory.superseded_by }}</a>
          </div>
        </div>
      </q-card-section>

      <q-card-section class="q-gutter-md">
        <q-input
          v-model="form.content"
          type="textarea"
          label="Content"
          autogrow
          autofocus
          outlined
          :rules="[(v) => !!(v && v.trim()) || 'Content is required']"
        />

        <div class="row q-col-gutter-md">
          <q-input
            v-if="!isEdit"
            v-model="form.agent"
            class="col"
            label="Agent"
            outlined
            dense
          />
          <q-input
            v-model="form.project"
            class="col"
            label="Project"
            outlined
            dense
            clearable
          />
          <q-select
            v-model="form.type"
            class="col"
            label="Type"
            outlined
            dense
            clearable
            :options="TYPES"
          />
        </div>

        <q-select
          v-model="form.tagNames"
          label="Tags"
          outlined
          dense
          multiple
          use-chips
          use-input
          new-value-mode="add-unique"
          :options="tagOptions"
          hint="Pick existing tags or type a new one and press Enter."
        />
      </q-card-section>

      <q-card-actions align="right">
        <q-btn flat label="Cancel" @click="close" />
        <q-btn
          color="primary"
          :label="isEdit ? 'Save' : 'Create'"
          :disable="!canSave"
          @click="save"
        />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>
