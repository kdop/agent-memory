import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '@/api/client'

// The search modes the server offers. `keyword` is the default and the only
// one that works without an embedding model.
export const SEARCH_MODES = ['keyword', 'semantic', 'hybrid']

// The review statuses a memory can carry (see CONTRACT.md, MemoryOut).
export const REVIEW_STATUSES = ['unverified', 'verified', 'flagged']

// The memory types the server accepts (server/schemas.py ALLOWED_MEMORY_TYPES).
// A constraint is a hard rule, so it gets the strongest badge.
export const MEMORY_TYPES = ['constraint', 'decision', 'lesson', 'note', 'preference']
export const TYPE_COLOR = {
  constraint: 'negative', decision: 'primary', lesson: 'teal', note: 'grey-7', preference: 'indigo-4',
}

// The shared memories view-state. The table, the search bar,
// and the multi-tag filter all drive it. The view-state fields (q/mode/tags/
// type/status/current/order/limit/offset) round-trip to the URL via
// composables/useUrlSync.js.
export const useMemoriesStore = defineStore('memories', () => {
  // ---- view-state (URL-synced) ----
  const q = ref('')            // full-text search string
  const mode = ref('keyword')  // search mode: keyword | semantic | hybrid
  const tags = ref([])         // array of tag names, OR-combined
  const agent = ref('')        // right-rail: exact agent filter ('' = any)
  const project = ref('')      // right-rail: exact project filter ('' = any)
  const type = ref('')         // right-rail: memory type filter ('' = any)
  const status = ref('')       // right-rail: review status filter ('' = any)
  const current = ref(false)   // right-rail: hide the memories a newer one supersedes
  const order = ref('date_desc') // '<field>_<asc|desc>'
  const limit = ref(100)       // page size
  const offset = ref(0)        // pagination offset

  // ---- results ----
  const results = ref([])      // MemoryOut[]
  const total = ref(0)         // X-Total-Count for the current query
  const loading = ref(false)
  const error = ref(null)
  // The mode the server used instead of the one asked for (X-Search-Fallback),
  // or null when it served what was asked.
  const fallback = ref(null)

  // Derived 1-based page number for pagination widgets.
  const page = computed(() => Math.floor(offset.value / limit.value) + 1)
  const pageCount = computed(() =>
    Math.max(1, Math.ceil(total.value / limit.value)),
  )

  // True when the query goes to /memories/search (semantic or hybrid with a
  // search string). That route ranks the whole set and has no pages.
  const ranked = computed(() => !!q.value && mode.value !== 'keyword')

  /** Jump to a 1-based page by recomputing offset. */
  function setPage(n) {
    offset.value = Math.max(0, (n - 1) * limit.value)
  }

  /**
   * Optimistically patch a single loaded row in place (in-place edit).
   * Deliberately does NOT re-filter: an edit that no longer matches the active
   * query stays visible until the next fetch() (eventual consistency).
   * @param {number} id
   * @param {object} fields  partial MemoryOut fields to merge onto the row
   */
  function patchRow(id, fields) {
    const row = results.value.find((r) => r.id === id)
    if (row) Object.assign(row, fields)
  }

  /** Drop a row from the loaded list immediately (optimistic delete). */
  function removeRow(id) {
    results.value = results.value.filter((r) => r.id !== id)
    total.value = Math.max(0, total.value - 1)
  }

  /** Clear all view-state back to defaults (no search / tags / filters / paging). */
  function reset() {
    q.value = ''
    mode.value = 'keyword'
    tags.value = []
    agent.value = ''
    project.value = ''
    type.value = ''
    status.value = ''
    current.value = false
    order.value = 'date_desc'
    offset.value = 0
  }

  /** Fetch the current view-state from the API and populate results + total. */
  async function fetch() {
    loading.value = true
    error.value = null
    fallback.value = null
    try {
      if (ranked.value) {
        // Semantic and hybrid live on /memories/search: one ranked list, no
        // pages, one tag filter, no type or status filter. The list route's keyword
        // search keeps its pages and filters, so keyword mode stays there.
        const { items, fallback: fb } = await api.searchMemories({
          q: q.value,
          mode: mode.value,
          tag: tags.value[0] || undefined,
          agent: agent.value || undefined,
          project: project.value || undefined,
          current: current.value || undefined,
          limit: limit.value,
        })
        results.value = items
        total.value = items.length
        fallback.value = fb
      } else {
        const { items, total: t } = await api.listMemories({
          q: q.value || undefined,
          tag: tags.value.length ? tags.value : undefined,
          agent: agent.value || undefined,
          project: project.value || undefined,
          type: type.value || undefined,
          status: status.value || undefined,
          current: current.value || undefined,
          order: order.value,
          limit: limit.value,
          offset: offset.value,
        })
        results.value = items
        total.value = t
      }
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
    q, mode, tags, agent, project, type, status, current, order, limit, offset,
    // results
    results, total, loading, error, fallback, ranked,
    // derived + helpers
    page, pageCount, setPage, patchRow, removeRow, reset, fetch,
  }
})
