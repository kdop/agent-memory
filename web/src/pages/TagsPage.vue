<script setup>
// Tags management view. D7 builds the table; D8 builds the detail/edit modal.
import { onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useTagsStore } from '@/stores/tags'
import { useAuthStore } from '@/stores/auth'

const tags = useTagsStore()
const auth = useAuthStore()
const { list, loading } = storeToRefs(tags)

onMounted(() => {
  if (auth.isAuthed) tags.fetch()
})
</script>

<template>
  <q-page class="q-pa-md">
    <div class="text-h6 q-mb-md">Tags</div>

    <!-- ============ D7: tags table ============
      Replace with the q-table over `tags.list` ({ name, count, description })
      with rename/describe/delete/merge actions calling the api client, then
      tags.fetch() to refresh. -->
    <!-- D7: tags table -->
    <q-card flat bordered>
      <q-inner-loading :showing="loading" />
      <q-list separator>
        <q-item v-for="t in list" :key="t.name">
          <q-item-section>
            <q-item-label>{{ t.name }}</q-item-label>
            <q-item-label caption>{{ t.description }}</q-item-label>
          </q-item-section>
          <q-item-section side>
            <q-badge>{{ t.count }}</q-badge>
          </q-item-section>
        </q-item>
        <q-item v-if="!loading && !list.length">
          <q-item-section class="text-grey">No tags.</q-item-section>
        </q-item>
      </q-list>
    </q-card>

    <!-- ============ D8: tag detail modal ============
      Mount the tag detail / edit dialog here (rename, re-describe, merge,
      detach). Open it from the D7 table rows. -->
    <!-- D8: tag detail modal -->
  </q-page>
</template>
