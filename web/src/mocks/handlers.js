// MSW v2 handlers implementing the FULL frozen contract (CONTRACT.md) against
// the in-memory seed (src/mocks/seed.js). Mutations (create/patch/delete, tag
// merge/detach/patch/delete) mutate the live seed so the UI sees real changes.
//
// Auth: any NON-EMPTY bearer token is accepted (mock); a missing/blank token →
// 401. GET /health needs no auth.
import { http, HttpResponse } from 'msw'
import { db, tagCount } from './seed.js'

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

/** Project a stored memory into MemoryOut (snippet only when searching). */
function toMemoryOut(m, q) {
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
  }
}

/** Sorted TagCount[] with live counts. */
function tagCounts() {
  return [...db.tags.values()]
    .map((t) => ({ name: t.name, count: tagCount(db, t.name), description: t.description }))
    .sort((a, b) => a.name.localeCompare(b.name))
}

// --------------------------------------------------------------- handlers ---

export const handlers = [
  // ---- GET /health (no auth) ----
  http.get('/health', () => HttpResponse.json({ status: 'ok' })),

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
    })
    return HttpResponse.json({ id }, { status: 201 })
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

    if (typeof body.content === 'string') { m.content = body.content; changes.push('content') }
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
    for (const m of db.memories) {
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
    for (const m of db.memories) {
      counts.set(m.agent, (counts.get(m.agent) || 0) + 1)
    }
    return HttpResponse.json([...counts.entries()].map(([agent, count]) => ({ agent, count })))
  }),

  // ---- GET /stats ----
  http.get('/stats', ({ request }) => {
    const denied = unauthorized(request)
    if (denied) return denied
    const agents = new Set(db.memories.map((m) => m.agent).filter(Boolean))
    const projects = new Set(db.memories.map((m) => m.project).filter(Boolean))
    const today = new Date().toISOString().slice(0, 10)
    const weekAgo = new Date(Date.now() - 7 * 86_400_000)
    const times = db.memories.map((m) => parseTs(m.timestamp)).filter((d) => !isNaN(d))
    return HttpResponse.json({
      total: db.memories.length,
      agents: agents.size,
      projects: projects.size,
      tags: db.tags.size,
      today: db.memories.filter((m) => m.timestamp && m.timestamp.slice(0, 10) === today).length,
      week: db.memories.filter((m) => parseTs(m.timestamp) >= weekAgo).length,
      oldest: times.length ? new Date(Math.min(...times)).toISOString() : null,
      newest: times.length ? new Date(Math.max(...times)).toISOString() : null,
    })
  }),
]

export default handlers
