// Typed API client for the agent-memory server. One function per endpoint in
// CONTRACT.md. Every call sends `Authorization: Bearer <token>` read live from
// the auth store (except GET /health, which is public).
//
// Base URL: same-origin in both dev and prod. In dev the MSW worker
// (src/mocks/) intercepts these requests; in prod they hit FastAPI behind the
// same origin. Centralised here so no component ever hardcodes a URL.
import { useAuthStore } from '@/stores/auth'

const BASE = '' // same-origin. Change here only if the API moves to another host.

/** Build a query string, supporting repeatable params via array values. */
function buildQuery(params) {
  const usp = new URLSearchParams()
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      for (const v of value) {
        if (v === undefined || v === null || v === '') continue
        usp.append(key, v)
      }
    } else {
      usp.append(key, value)
    }
  }
  const s = usp.toString()
  return s ? `?${s}` : ''
}

/** Auth header from the store, or {} when there is no token. */
function authHeaders() {
  const auth = useAuthStore()
  return auth.token ? { Authorization: `Bearer ${auth.token}` } : {}
}

/**
 * Core fetch wrapper.
 * @returns {Promise<{status,data,headers,ok}>} parsed JSON body + raw headers.
 * Throws an Error (with `.status` and `.data`) on non-2xx so callers can catch.
 */
async function request(method, path, { params, body, auth = true } = {}) {
  const url = BASE + path + buildQuery(params)
  const headers = { Accept: 'application/json' }
  if (auth) Object.assign(headers, authHeaders())
  const init = { method, headers }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }

  const res = await fetch(url, init)
  const text = await res.text()
  let data = null
  if (text) {
    try { data = JSON.parse(text) } catch { data = text }
  }

  if (!res.ok) {
    const err = new Error(`${method} ${path} → ${res.status}`)
    err.status = res.status
    err.data = data
    throw err
  }
  return { status: res.status, data, headers: res.headers, ok: res.ok }
}

export const api = {
  // ---- Memories ----------------------------------------------------------

  /**
   * GET /memories — list / search / filter (paginated).
   * @param {object} p
   * @param {string} [p.q]           full-text search (adds `snippet`, rank order)
   * @param {string[]} [p.tag]       repeatable, AND-combined
   * @param {string} [p.project]
   * @param {string} [p.agent]
   * @param {string} [p.type]
   * @param {'unverified'|'verified'|'flagged'} [p.status]  one review status
   * @param {boolean} [p.current]   true hides the memories a newer one supersedes
   * @param {number} [p.since_days]
   * @param {string} [p.since]
   * @param {string} [p.until]
   * @param {'date_desc'|'date_asc'} [p.order]
   * @param {number} [p.limit=100]
   * @param {number} [p.offset=0]
   * @returns {Promise<{items: object[], total: number}>} total from X-Total-Count
   */
  async listMemories(p = {}) {
    const { tag, ...rest } = p
    const res = await request('GET', '/memories', {
      params: { ...rest, tag }, // `tag` array → repeatable ?tag=a&tag=b
    })
    const total = Number(res.headers.get('X-Total-Count') ?? res.data.length)
    return { items: res.data, total }
  },

  /**
   * GET /memories/search — ranked search in one of three modes.
   * @param {object} p
   * @param {string} p.q
   * @param {'keyword'|'semantic'|'hybrid'} [p.mode='keyword']
   * @param {string} [p.project]
   * @param {string} [p.agent]
   * @param {string} [p.since]
   * @param {string} [p.tag]        one tag (the route takes a single one)
   * @param {boolean} [p.current]
   * @param {number} [p.limit=20]
   * @returns {Promise<{items: object[], fallback: string|null}>}
   *   `fallback` is the X-Search-Fallback header: the mode the server used
   *   instead of the one asked for (hybrid without a model → "keyword").
   */
  async searchMemories(p = {}) {
    const res = await request('GET', '/memories/search', { params: p })
    return { items: res.data, fallback: res.headers.get('X-Search-Fallback') || null }
  },

  /**
   * GET /memories/flagged — the memories the review flagged, newest review first.
   * @param {object} p
   * @param {string} [p.project]
   * @param {'reject'|'rewrite'} [p.verdict]  only that verdict
   * @param {'unverified'|'verified'|'flagged'} [p.status]  that status instead
   * @param {number} [p.limit=100]  0 = all
   * @returns {Promise<{items: object[], total: number}>} total from X-Total-Count
   */
  async flaggedMemories(p = {}) {
    const res = await request('GET', '/memories/flagged', { params: p })
    const total = Number(res.headers.get('X-Total-Count') ?? res.data.length)
    return { items: res.data, total }
  },

  /** POST /memories → { id }. */
  async createMemory(payload) {
    const res = await request('POST', '/memories', { body: payload })
    return res.data
  },

  /** GET /memories/bulk?ids=1&ids=2 → compact rows. */
  async bulkMemories(ids) {
    const res = await request('GET', '/memories/bulk', { params: { ids } })
    return res.data
  },

  /** GET /memories/{id} → MemoryOut (throws 404). */
  async getMemory(id) {
    const res = await request('GET', `/memories/${id}`)
    return res.data
  },

  /** PATCH /memories/{id} → { changes }. Only sent fields apply. */
  async patchMemory(id, patch) {
    const res = await request('PATCH', `/memories/${id}`, { body: patch })
    return res.data
  },

  /** DELETE /memories?ids=1&ids=2 → { deleted, missing }. */
  async deleteMemories(ids) {
    const res = await request('DELETE', '/memories', { params: { ids } })
    return res.data
  },

  // ---- Tags --------------------------------------------------------------

  /** GET /tags → TagCount[]. */
  async listTags() {
    const res = await request('GET', '/tags')
    return res.data
  },

  /** PATCH /tags/{name} → { name, description, count }. Rename may merge. */
  async patchTag(name, patch) {
    const res = await request('PATCH', `/tags/${encodeURIComponent(name)}`, { body: patch })
    return res.data
  },

  /** DELETE /tags/{name} → { removed, memories_affected }. */
  async deleteTag(name) {
    const res = await request('DELETE', `/tags/${encodeURIComponent(name)}`)
    return res.data
  },

  /** POST /tags/merge → { target, memories_affected, removed }. */
  async mergeTags({ sources, target, description }) {
    const res = await request('POST', '/tags/merge', {
      body: { sources, target, ...(description !== undefined ? { description } : {}) },
    })
    return res.data
  },

  /** POST /tags/{name}/detach → { detached }. Omit ids / [] = all memories. */
  async detachTag(name, memoryIds) {
    const res = await request('POST', `/tags/${encodeURIComponent(name)}/detach`, {
      body: { memory_ids: memoryIds ?? [] },
    })
    return res.data
  },

  // ---- Review ------------------------------------------------------------

  /**
   * POST /admin/review — review the unverified memories, oldest first.
   * → { scheduled: n } or { scheduled: 0, running: true } when one is already
   * running. 503 when the server has no review model.
   */
  async reviewCatchUp(limit) {
    const res = await request('POST', '/admin/review', {
      params: limit !== undefined ? { limit } : {},
    })
    return res.data
  },

  /**
   * POST /admin/review/{id} — review one memory now, replacing any earlier
   * verdict. → ReviewOut. 503 without a model, 404 when absent, 502 when the
   * model gave no verdict.
   */
  async reviewMemory(id) {
    const res = await request('POST', `/admin/review/${id}`)
    return res.data
  },

  // ---- Misc --------------------------------------------------------------

  /** GET /projects → [{ project, count }]. */
  async listProjects() {
    const res = await request('GET', '/projects')
    return res.data
  },

  /** GET /agents → [{ agent, count }]. */
  async listAgents() {
    const res = await request('GET', '/agents')
    return res.data
  },

  /** GET /stats → summary counts. */
  async stats() {
    const res = await request('GET', '/stats')
    return res.data
  },

  /** GET /health → { status, review_model?, catch_up? } (no auth). `review_model`
   *  is `reachable`, `unreachable` or `off` when the server reports it; `catch_up`
   *  is { total, done } while a catch-up runs, else null. */
  async health() {
    const res = await request('GET', '/health', { auth: false })
    return res.data
  },
}

export default api
