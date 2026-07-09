<script setup>
// D6 — create / edit form dialog. Pure form: it gathers values and emits
// `submit`; the table (MemoriesTable) owns the API orchestration so the
// "what to do after a mutation" policy lives in one place (optimistic edit vs
// refetch on create). `memory` prop present → edit mode; null → create mode.
// Tag editing uses the structured { name, description? } shape on save.
import { reactive, computed, watch } from 'vue'
import { useTagsStore } from '@/stores/tags'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  memory: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'submit'])

const tags = useTagsStore()
const TYPES = ['decision', 'code', 'lesson', 'note']

const isEdit = computed(() => !!props.memory)

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

// Repopulate the form each time the dialog opens.
watch(
  () => props.modelValue,
  (open) => { if (open) reset() },
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
        <div class="text-h6">{{ isEdit ? `Edit memory #${memory.id}` : 'New memory' }}</div>
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
