<script setup>
// Tags management view. D7 builds the table; D8 builds the detail/edit modal.
import { onMounted, ref } from 'vue'
import { useTagsStore } from '@/stores/tags'
import { useAuthStore } from '@/stores/auth'
import TagsTable from '@/components/TagsTable.vue'
import TagDetailModal from '@/components/TagDetailModal.vue'

const tags = useTagsStore()
const auth = useAuthStore()

// D8 modal state — opened from a D7 table row click.
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

    <!-- ============ D7: tags table ============ -->
    <!-- D7: tags table -->
    <TagsTable @open="openTag" />

    <!-- ============ D8: tag detail modal ============ -->
    <!-- D8: tag detail modal -->
    <TagDetailModal v-model="detailOpen" :tag="selectedTag" />
  </q-page>
</template>
