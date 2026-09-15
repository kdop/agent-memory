<script setup>
// Tags management view: the table and the detail/edit modal.
import { onMounted, ref } from 'vue'
import { useTagsStore } from '@/stores/tags'
import { useAuthStore } from '@/stores/auth'
import TagsTable from '@/components/TagsTable.vue'
import TagDetailModal from '@/components/TagDetailModal.vue'

const tags = useTagsStore()
const auth = useAuthStore()

// Modal state — opened from a table row click.
const detailOpen = ref(false)
const selectedTag = ref(null)

function openTag(tag) {
  selectedTag.value = tag
  detailOpen.value = true
}

onMounted(() => {
  if (auth.isAuthed) tags.fetch()
})
</script>

<template>
  <q-page class="q-pa-md">
    <div class="text-h6 q-mb-md">Tags</div>

    <!-- tags table -->
    <TagsTable @open="openTag" />

    <!-- tag detail modal -->
    <TagDetailModal v-model="detailOpen" :tag="selectedTag" />
  </q-page>
</template>
