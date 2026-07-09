import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '@/api/client'

// The shared memories view-state. This is the store D3 (table), D4 (search),
// and D5 (multi-tag filter) all drive. The view-state fields (q/tags/order/
// limit/offset) round-trip to the URL via composables/useUrlSync.js.
export const useMemoriesStore = defineStore('memories', () => {
  // ---- view-state (URL-synced) ----
  const q = ref('')            // D4: full-text search string
  const tags = ref([])         // D5: array of tag names, AND-combined
  const order = ref('date_desc') // D3: 'date_desc' | 'date_asc'
  const limit = ref(100)       // page size
  const offset = ref(0)        // pagination offset

  // ---- results ----
  const results = ref([])      // MemoryOut[]
  const total = ref(0)         // X-Total-Count for the current query
  const loading = ref(false)
  const error = ref(null)

  // Derived 1-based page number for pagination widgets.
  const page = computed(() => Math.floor(offset.value / limit.value) + 1)
  const pageCount = computed(() =>
    Math.max(1, Math.ceil(total.value / limit.value)),
  )

  /** Jump to a 1-based page by recomputing offset. */
  function setPage(n) {
    offset.value = Math.max(0, (n - 1) * limit.value)
  }

  /** Fetch the current view-state from the API and populate results + total. */
  async function fetch() {
    loading.value = true
    error.value = null
    try {
      const { items, total: t } = await api.listMemories({
        q: q.value || undefined,
        tag: tags.value.length ? tags.value : undefined,
        order: order.value,
        limit: limit.value,
        offset: offset.value,
      })
      results.value = items
      total.value = t
    } catch (err) {
      error.value = err
      results.value = []
      total.value = 0
    } finally {
      loading.value = false
    }
  }

  return {
    // view-state
    q, tags, order, limit, offset,
    // results
    results, total, loading, error,
    // derived + helpers
    page, pageCount, setPage, fetch,
  }
})
