import { watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMemoriesStore } from '@/stores/memories'

// Two-way sync between the memories view-state and the URL query string, so
// copy/paste + refresh restores the exact view (requirement 1e).
//
// URL param names (documented contract for the whole dashboard):
//   q       → memories.q       (search string; omitted when empty)
//   tags    → memories.tags    (comma-joined tag names, OR-combined)
//   agent   → memories.agent   (exact agent filter; omitted when empty)
//   project → memories.project (exact project filter; omitted when empty)
//   order   → memories.order   ('<field>_<asc|desc>'; omitted when default)
//   page    → derived 1-based page from memories.offset / memories.limit
//             (omitted when page 1)
// `limit` is intentionally NOT in the URL — it is a fixed page size (100).
const DEFAULTS = { order: 'date_desc' }

/** Build a route-query object from the current store state. */
function stateToQuery(store) {
  const query = {}
  if (store.q) query.q = store.q
  if (store.tags.length) query.tags = store.tags.join(',')
  if (store.agent) query.agent = store.agent
  if (store.project) query.project = store.project
  if (store.order && store.order !== DEFAULTS.order) query.order = store.order
  if (store.page > 1) query.page = String(store.page)
  return query
}

/** Apply a route-query object onto the store (hydration). */
function queryToState(store, query) {
  store.q = typeof query.q === 'string' ? query.q : ''
  store.tags = typeof query.tags === 'string' && query.tags.length
    ? query.tags.split(',').filter(Boolean)
    : []
  store.agent = typeof query.agent === 'string' ? query.agent : ''
  store.project = typeof query.project === 'string' ? query.project : ''
  store.order = typeof query.order === 'string' && query.order ? query.order : DEFAULTS.order
  const page = Math.max(1, parseInt(query.page, 10) || 1)
  store.offset = (page - 1) * store.limit
}

/** Shallow equality of two flat query objects. */
function sameQuery(a, b) {
  const ak = Object.keys(a)
  const bk = Object.keys(b)
  if (ak.length !== bk.length) return false
  return ak.every((k) => a[k] === b[k])
}

/**
 * Wire up URL <-> store syncing. Call once from MemoriesPage setup().
 * Returns { hydrate } — call it in onMounted BEFORE the first fetch so the store
 * reflects the incoming URL.
 */
export function useUrlSync() {
  const route = useRoute()
  const router = useRouter()
  const store = useMemoriesStore()

  let applyingFromUrl = false

  /** Load the store FROM the current URL (call before the initial fetch). */
  function hydrate() {
    applyingFromUrl = true
    queryToState(store, route.query)
    applyingFromUrl = false
  }

  // State → URL: push view-state changes into route.query (replace, no history spam).
  watch(
    () => [store.q, store.tags, store.agent, store.project, store.order, store.offset, store.limit],
    () => {
      if (applyingFromUrl) return
      const next = stateToQuery(store)
      if (!sameQuery(next, route.query)) {
        router.replace({ query: next })
      }
    },
    { deep: true },
  )

  // URL → State: e.g. back/forward navigation re-hydrates the store, then refetch.
  watch(
    () => route.query,
    (query) => {
      const current = stateToQuery(store)
      if (sameQuery(current, query)) return // change originated from the store
      applyingFromUrl = true
      queryToState(store, query)
      applyingFromUrl = false
      store.fetch()
    },
  )

  return { hydrate }
}
