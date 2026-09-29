// MSW v2 handlers implementing the FULL frozen contract (CONTRACT.md) against
// the in-memory seed (src/mocks/seed.js). Mutations (create/patch/delete, tag
// merge/detach/patch/delete) mutate the live seed so the UI sees real changes.
//
// Auth: any NON-EMPTY bearer token is accepted (mock); a missing/blank token →
// 401. GET /health needs no auth.
import { http, HttpResponse } from 'msw'
import { db, stamp, tagCount } from './seed.js'

// ---------------------------------------------------------------- helpers ---

/** 401 unless the request carries a non-empty bearer token. */
function unauthorized(request) {
  const h = request.headers.get('Authorization') || ''
  const m = /^Bearer\s+(.+)$/i.exec(h)
  if (!m || !m[1].trim()) {
    return HttpResponse.json({ detail: 'Missing or invalid token' }, { status: 401 })
  }
  return null
}

const parseTs = (ts) => new Date(String(ts).replace(' ', 'T'))

/** Project a stored memory into MemoryOut (snippet only when searching;
 *  `score` only when the caller passes one). */
function toMemoryOut(m, q, score = null) {
  let snippet = null
  if (q) {
    const idx = m.content.toLowerCase().indexOf(q.toLowerCase())
    if (idx >= 0) {
      const start = Math.max(0, idx - 20)
      const end = Math.min(m.content.length, idx + q.length + 20)
      const before = (start > 0 ? '… ' : '') + m.content.slice(start, idx)
      const match = m.content.slice(idx, idx + q.length)
      const after = m.content.slice(idx + q.length, end) + (end < m.content.length ? ' …' : '')
      snippet = `${before}→${match}←${after}`
    }
  }
  return {
    id: m.id,
    timestamp: m.timestamp ?? null,
    agent: m.agent,
    project: m.project ?? null,
    content: m.content,
    type: m.type ?? null,
    tags: [...m.tags].sort(),
    snippet,
    score,
    review: m.review ?? null,
    review_status: m.review_status ?? 'unverified',
    supersedes: m.supersedes ?? null,
    superseded_by: m.superseded_by ?? null,
    archived_at: m.archived_at ?? null,
  }
}

// How many days the mock server keeps an archived memory, as GET /health says.
const ARCHIVE_DAYS = 30

/** The live memories, or with ?archived=true only the archived ones. */
function archivedFilter(rows, sp) {
  const archived = sp.get('archived')
  const want = archived === 'true' || archived === '1'
  return rows.filter((m) => Boolean(m.archived_at) === want)
}

const live = () => db.memories.filter((m) => !m.archived_at)

/** Store a mock verdict the way the server does: the status follows it, and
 *  a reject under rule 2 or 4 archives the memory. */
function applyVerdict(m) {
  m.review = mockVerdict(m)
  m.review_status = m.review.verdict === 'approve' ? 'verified' : 'flagged'
  if (m.review.verdict === 'reject' && [2, 4].includes(m.review.rule) && !m.archived_at) {
    m.archived_at = stamp(new Date())
  }
}

/** Keep to one review status and/or hide the superseded rows. */
function statusFilters(rows, sp) {
  const status = sp.get('status')
  const current = sp.get('current')
  if (status) rows = rows.filter((m) => (m.review_status ?? 'unverified') === status)
  if (current === 'true' || current === '1') rows = rows.filter((m) => !m.superseded_by)
  return rows
}

/** Mock verdict for the review routes: alternate approve / reject / rewrite. */
function mockVerdict(m) {
  const n = m.id % 3
  if (n === 0) {
    return { verdict: 'approve', rule: null, reason: 'A durable decision with its reason.',
             rewrite: null, duplicate_of: null, tags: [], supersedes: null }
  }
  if (n === 1) {
    return { verdict: 'reject', rule: 2, reason: 'A diary line: git history already has it.',
             rewrite: null, duplicate_of: null, tags: [], supersedes: null }
  }
  return { verdict: 'rewrite', rule: 3, reason: 'Says what was done, not why.',
           rewrite: `${m.content}: kept because it halved the cold-start time.`,
           duplicate_of: null, tags: m.tags.slice(0, 2), supersedes: null }
}

// The running catch-up's progress, as GET /health reports it: { kind, total,
// done }, or null when none runs. Memories first, then tags. Only one runs
// at a time.
let catchUp = null
// How long the mock takes per memory, so the progress bar can be seen.
const MOCK_REVIEW_MS = 2000

/** Sorted TagCount[] with live counts and review status. */
function tagCounts() {
  return [...db.tags.values()]
    .map((t) => ({ name: t.name, count: tagCount(db, t.name), description: t.description,
                   review_status: t.review_status ?? 'unverified' }))
    .sort((a, b) => a.name.localeCompare(b.name))
}

/** Move every link from tag `source` to `target` and delete `source`. */
function mergeInto(source, target) {
  let affected = 0
  for (const m of db.memories) {
    if (!m.tags.includes(source)) continue
    m.tags = m.tags.filter((t) => t !== source)
    if (!m.tags.includes(target)) m.tags.push(target)
    m.tags.sort()
    affected++
  }
  db.tags.delete(source)
  return affected
}

// --------------------------------------------------------------- handlers ---

export const handlers = [
  // ---- GET /health (no auth) ----
  http.get('/health', () =>
    HttpResponse.json({ status: 'ok', review_model: 'reachable', catch_up: catchUp,
                        archive_days: ARCHIVE_DAYS })),

  // ---- GET /memories/bulk (before /memories/:id) ----
  http.get('/memories/bulk', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const url = new URL(request.url)
    const ids = url.searchParams.getAll('ids').map(Number)
    const rows = db.memories
      .filter((m) => ids.includes(m.id))
      .map((m) => ({ id: m.id, agent: m.agent, project: m.project ?? null, type: m.type ?? null, content: m.content }))
    return HttpResponse.json(rows)
  }),

  // ---- GET /memories/search (ranked; keyword / semantic / hybrid) ----
  http.get('/memories/search', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const sp = new URL(request.url).searchParams
    const q = sp.get('q') || ''
    const mode = sp.get('mode') || 'keyword'
    const limit = Math.max(0, parseInt(sp.get('limit') ?? '20', 10) || 0)
    const project = sp.get('project')
    const agent = sp.get('agent')
    const tag = sp.get('tag')
    let rows = db.memories.slice()
    if (project != null) rows = rows.filter((m) => (m.project ?? '') === project)
    if (agent != null) rows = rows.filter((m) => m.agent === agent)
    if (tag) rows = rows.filter((m) => m.tags.includes(tag))
    rows = archivedFilter(statusFilters(rows, sp), sp)
    const words = q.toLowerCase().split(/\s+/).filter(Boolean)
    // Score: keyword = share of words present; semantic/hybrid = the same plus
    // a little for shared letters, so the ranked list differs a bit.
    const scored = rows
      .map((m) => {
        const text = m.content.toLowerCase()
        const hits = words.filter((w) => text.includes(w)).length
        let score = words.length ? hits / words.length : 0
        if (mode !== 'keyword' && words.length) {
          // A stand-in for a cosine: the word share plus a fixed per-row part.
          score = 0.55 * score + 0.45 * (((m.id * 37) % 100) / 100)
        }
        return [score, m]
      })
      .filter(([score, m]) => (mode === 'keyword' ? words.every((w) => m.content.toLowerCase().includes(w)) : score > 0.35))
      .sort((a, b) => b[0] - a[0] || b[1].id - a[1].id)
    const page = (limit ? scored.slice(0, limit) : scored)
      .map(([score, m]) => toMemoryOut(m, mode === 'semantic' ? '' : q, Number(score.toFixed(4))))
    // The mock has a model, so hybrid is served as asked; ?fallback=1 shows
    // the fallback note the real server sends when it has none.
    const headers = sp.get('fallback') ? { 'X-Search-Fallback': 'keyword' } : {}
    return HttpResponse.json(page, { headers })
  }),

  // ---- GET /memories/flagged (reject / rewrite, newest review first) ----
  http.get('/memories/flagged', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const sp = new URL(request.url).searchParams
    const verdict = sp.get('verdict')
    const status = sp.get('status')
    const project = sp.get('project')
    const limit = Math.max(0, parseInt(sp.get('limit') ?? '100', 10) || 0)
    let rows = archivedFilter(db.memories, sp)
    if (project != null) rows = rows.filter((m) => (m.project ?? '') === project)
    if (status) rows = rows.filter((m) => (m.review_status ?? 'unverified') === status)
    else rows = rows.filter((m) => m.review && ['reject', 'rewrite'].includes(m.review.verdict))
    if (verdict) rows = rows.filter((m) => m.review?.verdict === verdict)
    rows.sort((a, b) => b.id - a.id)
    const total = rows.length
    const page = (limit ? rows.slice(0, limit) : rows).map((m) => toMemoryOut(m, ''))
    return HttpResponse.json(page, { headers: { 'X-Total-Count': String(total) } })
  }),

  // ---- POST /admin/review (catch-up) ----
  http.post('/admin/review', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    if (catchUp) return HttpResponse.json({ scheduled: 0, tags: 0, running: true })
    const sp = new URL(request.url).searchParams
    const limit = Math.max(0, parseInt(sp.get('limit') ?? '50', 10) || 0)
    const pending = live().filter((m) => (m.review_status ?? 'unverified') === 'unverified')
    const ids = (limit ? pending.slice(0, limit) : pending).map((m) => m.id)
    const unverifiedTags = [...db.tags.values()].filter((t) => (t.review_status ?? 'unverified') === 'unverified')
    const tagNames = (limit ? unverifiedTags.slice(0, limit) : unverifiedTags).map((t) => t.name)
    // Verdicts land one by one, a little later, as on the real server:
    // the memories, then the tags (each tag is kept).
    const halves = [['memories', ids, (id) => {
      const m = db.memories.find((x) => x.id === id)
      if (m) applyVerdict(m)
    }], ['tags', tagNames, (name) => {
      const t = db.tags.get(name)
      if (t) t.review_status = 'verified'
    }]].filter(([, items]) => items.length)
    const run = (h) => {
      if (h >= halves.length) { catchUp = null; return }
      const [kind, items, act] = halves[h]
      catchUp = { kind, total: items.length, done: 0 }
      const step = () => {
        act(items[catchUp.done])
        catchUp = { kind, total: items.length, done: catchUp.done + 1 }
        if (catchUp.done < catchUp.total) setTimeout(step, MOCK_REVIEW_MS)
        else run(h + 1)
      }
      setTimeout(step, MOCK_REVIEW_MS)
    }
    run(0)
    return HttpResponse.json({ scheduled: ids.length, tags: tagNames.length })
  }),

  // ---- POST /admin/review/:id (review one memory now) ----
  http.post('/admin/review/:id', ({ request, params }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const m = db.memories.find((x) => x.id === Number(params.id))
    if (!m) return HttpResponse.json({ detail: 'Not found' }, { status: 404 })
    applyVerdict(m)
    return HttpResponse.json(m.review)
  }),

  // ---- GET /memories (list / search / filter, paginated) ----
  http.get('/memories', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const url = new URL(request.url)
    const sp = url.searchParams

    const q = sp.get('q') || ''
    const tagFilters = sp.getAll('tag').filter(Boolean) // AND
    const project = sp.get('project')
    const agent = sp.get('agent')
    const type = sp.get('type')
    const sinceDays = sp.get('since_days')
    const since = sp.get('since')
    const until = sp.get('until')
    const order = sp.get('order') || 'date_desc'
    const limit = Math.max(0, parseInt(sp.get('limit') ?? '100', 10) || 100)
    const offset = Math.max(0, parseInt(sp.get('offset') ?? '0', 10) || 0)

    let rows = db.memories.slice()

    if (q) rows = rows.filter((m) => m.content.toLowerCase().includes(q.toLowerCase()))
    if (tagFilters.length) rows = rows.filter((m) => tagFilters.every((t) => m.tags.includes(t)))
    if (project != null) rows = rows.filter((m) => (m.project ?? '') === project)
    if (agent != null) rows = rows.filter((m) => m.agent === agent)
    if (type != null) rows = rows.filter((m) => (m.type ?? '') === type)
    rows = archivedFilter(statusFilters(rows, sp), sp)

    if (sinceDays != null && sinceDays !== '') {
      // Single calendar day N days ago (UTC). Overrides since/until.
      const n = parseInt(sinceDays, 10) || 0
      const day = new Date(Date.now() - n * 86_400_000).toISOString().slice(0, 10)
      rows = rows.filter((m) => m.timestamp && m.timestamp.slice(0, 10) === day)
    } else {
      if (since) rows = rows.filter((m) => m.timestamp && parseTs(m.timestamp) >= new Date(since))
      if (until) rows = rows.filter((m) => m.timestamp && parseTs(m.timestamp) <= new Date(until))
    }

    // Ordering: q → relevance (earliest match, then recency); else by date.
    if (q) {
      rows.sort((a, b) => {
        const ai = a.content.toLowerCase().indexOf(q.toLowerCase())
        const bi = b.content.toLowerCase().indexOf(q.toLowerCase())
        if (ai !== bi) return ai - bi
        return parseTs(b.timestamp) - parseTs(a.timestamp)
      })
    } else if (order === 'archived_desc') {
      rows.sort((a, b) => parseTs(b.archived_at) - parseTs(a.archived_at) || b.id - a.id)
    } else {
      rows.sort((a, b) =>
        order === 'date_asc'
          ? parseTs(a.timestamp) - parseTs(b.timestamp)
          : parseTs(b.timestamp) - parseTs(a.timestamp),
      )
    }

    const total = rows.length
    const page = rows.slice(offset, offset + limit).map((m) => toMemoryOut(m, q))
    return HttpResponse.json(page, { headers: { 'X-Total-Count': String(total) } })
  }),

  // ---- POST /memories (create) ----
  http.post('/memories', async ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const body = await request.json()
    const inTags = body.tags || []
    if (inTags.some((t) => !t || !String(t.name || '').trim())) {
      return HttpResponse.json({ detail: 'Empty tag name' }, { status: 422 })
    }
    const id = db.memories.reduce((max, m) => Math.max(max, m.id), 0) + 1
    for (const t of inTags) {
      const name = t.name
      if (!db.tags.has(name)) db.tags.set(name, { name, description: t.description || name })
    }
    db.memories.push({
      id,
      timestamp: new Date().toISOString().replace('T', ' ').replace(/\.\d+Z$/, '+00:00'),
      agent: body.agent ?? null,
      project: body.project ?? null,
      content: body.content,
      type: body.type ?? null,
      tags: inTags.map((t) => t.name).sort(),
      review_status: 'unverified',
      review: null,
      supersedes: null,
      superseded_by: null,
      archived_at: null,
    })
    return HttpResponse.json({ id, warnings: [] }, { status: 201 })
  }),

  // ---- POST /memories/:id/restore ----
  http.post('/memories/:id/restore', ({ request, params }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const m = db.memories.find((x) => x.id === Number(params.id))
    if (!m) return HttpResponse.json({ detail: 'Not found' }, { status: 404 })
    const restored = Boolean(m.archived_at)
    m.archived_at = null
    return HttpResponse.json({ id: m.id, restored })
  }),

  // ---- GET /memories/:id ----
  http.get('/memories/:id', ({ request, params }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const m = db.memories.find((x) => x.id === Number(params.id))
    if (!m) return HttpResponse.json({ detail: 'Not found' }, { status: 404 })
    return HttpResponse.json(toMemoryOut(m, ''))
  }),

  // ---- PATCH /memories/:id (edit) ----
  http.patch('/memories/:id', async ({ request, params }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const m = db.memories.find((x) => x.id === Number(params.id))
    if (!m) return HttpResponse.json({ detail: 'Not found' }, { status: 404 })
    const body = await request.json()
    const changes = []

    if (typeof body.content === 'string') {
      m.content = body.content
      changes.push('content')
      // New text, new check: the verdict was about the old text.
      m.review = null
      m.review_status = 'unverified'
      m.supersedes = null
      m.archived_at = null // new text is live again, and waits for its check
    }
    if (typeof body.project === 'string') { m.project = body.project === '' ? null : body.project; changes.push('project') }
    if (typeof body.type === 'string') { m.type = body.type === '' ? null : body.type; changes.push('type') }

    const ensureTag = (t) => {
      if (!db.tags.has(t.name)) db.tags.set(t.name, { name: t.name, description: t.description || t.name })
    }
    if (Array.isArray(body.set_tags)) {
      body.set_tags.forEach(ensureTag)
      m.tags = body.set_tags.map((t) => t.name).sort()
      changes.push(`set_tags: ${m.tags.join(',') || '(none)'}`)
    }
    if (Array.isArray(body.add_tags)) {
      for (const t of body.add_tags) {
        ensureTag(t)
        if (!m.tags.includes(t.name)) { m.tags.push(t.name); changes.push(`+tags: ${t.name}`) }
      }
      m.tags.sort()
    }
    if (Array.isArray(body.remove_tags)) {
      for (const name of body.remove_tags) {
        if (m.tags.includes(name)) { m.tags = m.tags.filter((x) => x !== name); changes.push(`-tags: ${name}`) }
      }
    }
    return HttpResponse.json({ changes })
  }),

  // ---- DELETE /memories?ids=1&ids=2 ----
  http.delete('/memories', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const url = new URL(request.url)
    const ids = url.searchParams.getAll('ids').map(Number)
    const missing = []
    let deleted = 0
    for (const id of ids) {
      const idx = db.memories.findIndex((m) => m.id === id)
      if (idx === -1) { missing.push(id); continue }
      db.memories.splice(idx, 1)
      deleted++
    }
    return HttpResponse.json({ deleted, missing })
  }),

  // ---- GET /tags ----
  http.get('/tags', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    return HttpResponse.json(tagCounts())
  }),

  // ---- GET /tags/flagged: the tag proposals that wait ----
  http.get('/tags/flagged', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    return HttpResponse.json(db.tagProposals.filter((p) => !p.resolved && db.tags.has(p.tag)))
  }),

  // ---- POST /tags/proposals/:id/apply and /reject ----
  http.post('/tags/proposals/:id/:action', ({ request, params }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const p = db.tagProposals.find((x) => x.id === Number(params.id))
    if (!p || !['apply', 'reject'].includes(params.action)) {
      return HttpResponse.json({ detail: `Proposal #${params.id} not found` }, { status: 404 })
    }
    if (p.resolved) {
      return HttpResponse.json({ detail: `Proposal #${p.id} is already ${p.resolved}` },
                               { status: 409 })
    }
    const tag = db.tags.get(p.tag)
    if (!tag) {
      return HttpResponse.json({ detail: `Proposal #${p.id}: the tag '${p.tag}' no longer exists` },
                               { status: 409 })
    }
    if (params.action === 'reject') {
      p.resolved = 'rejected'
      tag.review_status = 'verified'
      return HttpResponse.json({ proposal: p })
    }
    let result
    if (p.verdict === 'merge') {
      if (!db.tags.has(p.into)) {
        return HttpResponse.json({ detail: `Proposal #${p.id}: the tag '${p.into}' to merge into no longer exists` },
                                 { status: 409 })
      }
      result = { target: p.into, memories_affected: mergeInto(p.tag, p.into), removed: [p.tag] }
    } else if (p.verdict === 'rename') {
      if (db.tags.has(p.new_name)) mergeInto(p.tag, p.new_name)
      else {
        db.tags.set(p.new_name, { ...tag, name: p.new_name })
        mergeInto(p.tag, p.new_name)
      }
      db.tags.get(p.new_name).review_status = 'verified'
      result = { name: p.new_name, description: db.tags.get(p.new_name).description,
                 count: tagCount(db, p.new_name) }
    } else {
      const affected = tagCount(db, p.tag)
      for (const m of db.memories) m.tags = m.tags.filter((t) => t !== p.tag)
      db.tags.delete(p.tag)
      result = { removed: p.tag, memories_affected: affected }
    }
    p.resolved = 'applied'
    return HttpResponse.json({ proposal: p, result })
  }),

  // ---- POST /tags/merge (before /tags/:name/detach & /tags/:name) ----
  http.post('/tags/merge', async ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const { sources = [], target, description } = await request.json()
    if (!db.tags.has(target)) db.tags.set(target, { name: target, description: description || target })
    else if (description !== undefined) db.tags.get(target).description = description

    const affected = new Set()
    for (const m of db.memories) {
      let touched = false
      for (const s of sources) {
        if (m.tags.includes(s)) {
          m.tags = m.tags.filter((t) => t !== s)
          touched = true
        }
      }
      if (touched) {
        if (!m.tags.includes(target)) m.tags.push(target)
        m.tags.sort()
        affected.add(m.id)
      }
    }
    for (const s of sources) db.tags.delete(s)
    return HttpResponse.json({ target, memories_affected: affected.size, removed: sources })
  }),

  // ---- POST /tags/:name/detach ----
  http.post('/tags/:name/detach', async ({ request, params }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const name = decodeURIComponent(params.name)
    if (!db.tags.has(name)) return HttpResponse.json({ detail: 'Not found' }, { status: 404 })
    const body = await request.json().catch(() => ({}))
    const ids = body?.memory_ids
    const targets = ids && ids.length ? new Set(ids) : null // null = all
    let detached = 0
    for (const m of db.memories) {
      if (targets && !targets.has(m.id)) continue
      if (m.tags.includes(name)) { m.tags = m.tags.filter((t) => t !== name); detached++ }
    }
    return HttpResponse.json({ detached })
  }),

  // ---- PATCH /tags/:name (rename / re-describe; collision → merge) ----
  http.patch('/tags/:name', async ({ request, params }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const name = decodeURIComponent(params.name)
    const tag = db.tags.get(name)
    if (!tag) return HttpResponse.json({ detail: 'Not found' }, { status: 404 })
    const body = await request.json()

    let finalName = name
    if (typeof body.name === 'string' && body.name && body.name !== name) {
      const collide = [...db.tags.keys()].find((k) => k.toLowerCase() === body.name.toLowerCase())
      if (collide && collide !== name) {
        // Merge self → existing target.
        for (const m of db.memories) {
          if (m.tags.includes(name)) {
            m.tags = m.tags.filter((t) => t !== name)
            if (!m.tags.includes(collide)) m.tags.push(collide)
            m.tags.sort()
          }
        }
        db.tags.delete(name)
        finalName = collide
        if (typeof body.description === 'string') db.tags.get(collide).description = body.description
      } else {
        // Plain rename.
        db.tags.delete(name)
        for (const m of db.memories) {
          if (m.tags.includes(name)) { m.tags = m.tags.map((t) => (t === name ? body.name : t)).sort() }
        }
        const desc = typeof body.description === 'string' ? body.description : tag.description
        db.tags.set(body.name, { name: body.name, description: desc })
        finalName = body.name
      }
    } else if (typeof body.description === 'string') {
      tag.description = body.description
    }

    const t = db.tags.get(finalName)
    return HttpResponse.json({ name: finalName, description: t.description, count: tagCount(db, finalName) })
  }),

  // ---- DELETE /tags/:name ----
  http.delete('/tags/:name', ({ request, params }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const name = decodeURIComponent(params.name)
    if (!db.tags.has(name)) return HttpResponse.json({ detail: 'Not found' }, { status: 404 })
    let affected = 0
    for (const m of db.memories) {
      if (m.tags.includes(name)) { m.tags = m.tags.filter((t) => t !== name); affected++ }
    }
    db.tags.delete(name)
    return HttpResponse.json({ removed: name, memories_affected: affected })
  }),

  // ---- GET /projects ----
  http.get('/projects', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const counts = new Map()
    for (const m of live()) {
      const p = m.project ?? null
      counts.set(p, (counts.get(p) || 0) + 1)
    }
    return HttpResponse.json([...counts.entries()].map(([project, count]) => ({ project, count })))
  }),

  // ---- GET /agents ----
  http.get('/agents', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const counts = new Map()
    for (const m of live()) {
      counts.set(m.agent, (counts.get(m.agent) || 0) + 1)
    }
    return HttpResponse.json([...counts.entries()].map(([agent, count]) => ({ agent, count })))
  }),

  // ---- GET /stats ----
  http.get('/stats', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const rows = live()
    const agents = new Set(rows.map((m) => m.agent).filter(Boolean))
    const projects = new Set(rows.map((m) => m.project).filter(Boolean))
    const today = new Date().toISOString().slice(0, 10)
    const weekAgo = new Date(Date.now() - 7 * 86_400_000)
    const times = rows.map((m) => parseTs(m.timestamp)).filter((d) => !isNaN(d))
    return HttpResponse.json({
      total: rows.length,
      agents: agents.size,
      projects: projects.size,
      tags: db.tags.size,
      today: rows.filter((m) => m.timestamp && m.timestamp.slice(0, 10) === today).length,
      week: rows.filter((m) => parseTs(m.timestamp) >= weekAgo).length,
      oldest: times.length ? new Date(Math.min(...times)).toISOString() : null,
      newest: times.length ? new Date(Math.max(...times)).toISOString() : null,
      archived: db.memories.length - rows.length,
    })
  }),
]

export default handlers
