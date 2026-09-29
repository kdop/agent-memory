import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'

// All tags with counts + descriptions + review status. Feeds the right-rail
// filter and the tags management view. Kept simple: one `fetch()` that
// mutations can re-run after merge/delete/patch. It also loads the tag
// review's proposals that wait (GET /tags/flagged), keyed by tag name, so the
// Tags page can show each flagged tag's proposal next to it.
export const useTagsStore = defineStore('tags', () => {
  const list = ref([])       // TagCount[]  { name, count, description, review_status }
  const proposals = ref({})  // tag name → { id, verdict, into, new_name, reason }
  const loading = ref(false)
  const error = ref(null)

  async function fetch() {
    loading.value = true
    error.value = null
    try {
      const data = await api.listTags()
      // Client sorts alphabetically by name (per CONTRACT.md).
      list.value = [...data].sort((a, b) => a.name.localeCompare(b.name))
      // The proposals are extra: a server without them still lists tags.
      try {
        const waiting = await api.tagProposals()
        proposals.value = Object.fromEntries((waiting || []).map((p) => [p.tag, p]))
      } catch {
        proposals.value = {}
      }
    } catch (err) {
      error.value = err
      list.value = []
    } finally {
      loading.value = false
    }
  }

  return { list, proposals, loading, error, fetch }
})
