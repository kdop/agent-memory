import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'

// All tags with counts + descriptions. Feeds the right-rail filter and the
// tags management view. Kept simple: one `fetch()` that mutations can
// re-run after merge/delete/patch.
export const useTagsStore = defineStore('tags', () => {
  const list = ref([])       // TagCount[]  { name, count, description }
  const loading = ref(false)
  const error = ref(null)

  async function fetch() {
    loading.value = true
    error.value = null
    try {
      const data = await api.listTags()
      // Client sorts alphabetically by name (per CONTRACT.md).
      list.value = [...data].sort((a, b) => a.name.localeCompare(b.name))
    } catch (err) {
      error.value = err
      list.value = []
    } finally {
      loading.value = false
    }
  }

  return { list, loading, error, fetch }
})
