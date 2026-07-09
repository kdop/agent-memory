// Deterministic mock seed for the dashboard. No Math.random — a fixed LCG makes
// every run identical, so screenshots/tests are stable. Produces ~250 memories
// and ~40 tags across the agents/projects/types named in CONTRACT.md, spread
// over the last ~60 days (enough for 3 pages, search, AND-filter, tag merge).

// --- deterministic PRNG (mulberry32) ---
function makeRng(seed) {
  let a = seed >>> 0
  return function rng() {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const AGENTS = ['agent-a', 'clu']
const PROJECTS = ['agent-memory', 'project-a', null]
const TYPES = ['decision', 'code', 'lesson', 'note']

// ~40 tags, each with a description.
const TAG_DEFS = [
  ['auth', 'authentication flow'],
  ['db', 'database + schema'],
  ['cli', 'command-line surface'],
  ['migration', 'schema / data migrations'],
  ['api', 'HTTP API server'],
  ['mcp', 'MCP server surface'],
  ['store', 'store abstraction seam'],
  ['sqlite', 'SQLite backend'],
  ['config', 'configuration + env resolution'],
  ['tests', 'test suite'],
  ['ci', 'continuous integration'],
  ['coverage', 'coverage metrics'],
  ['docs', 'documentation'],
  ['perf', 'performance'],
  ['refactor', 'code cleanup'],
  ['bug', 'defect / fix'],
  ['ui', 'dashboard UI'],
  ['vue', 'Vue frontend'],
  ['quasar', 'Quasar components'],
  ['router', 'client routing'],
  ['pinia', 'state management'],
  ['msw', 'mock service worker'],
  ['search', 'full-text search'],
  ['tags', 'tag management'],
  ['pagination', 'paged listing'],
  ['filter', 'filtering + query params'],
  ['export', 'data export'],
  ['import', 'data import'],
  ['backup', 'db backup safety'],
  ['security', 'security + tokens'],
  ['token', 'bearer token auth'],
  ['deploy', 'deployment'],
  ['logging', 'logging protocol'],
  ['persona', 'persona MCP'],
  ['project-a', 'project-a project'],
  ['dogfood', 'dogfooding memory'],
  ['schema', 'db schema design'],
  ['index', 'db indexing'],
  ['cache', 'caching'],
  ['error', 'error handling'],
]

const CONTENT_STEMS = [
  'Repointed DB path resolution to prefer AGENT_MEMORY_DB',
  'Added X-Total-Count header to the memories list endpoint',
  'Split the store contract so ApiStore and SqliteStore stay in lockstep',
  'Introduced AND multi-tag filtering on GET /memories',
  'Fixed a snippet ranking bug when the query spanned word boundaries',
  'Wrote the coverage gate that fails a PR lowering coverage',
  'Merged duplicate auth tags into a single canonical tag',
  'Documented the frozen dashboard API contract',
  'Cached the tag counts to avoid a full table scan per request',
  'Handled the 503 case when the server has no token configured',
  'Added pagination with limit/offset and a total count',
  'Scoped the stdlib-only rule to the client surface only',
  'Backed up memory.db before the schema migration',
  'Seeded the MSW mock with deterministic fixtures',
  'Wired the login gate to validate tokens against GET /tags',
  'Refactored the CLI shim onto agent_memory.cli:main',
  'Added a detach endpoint to remove a tag from selected memories',
  'Normalised tag names to lowercase on merge',
  'Exposed /stats with agent/project/tag rollups',
  'Traced a slow query to a missing index on timestamp',
]

/** Build the mutable in-memory dataset the handlers operate on. */
export function buildSeed() {
  const rng = makeRng(0x51ed) // fixed seed → identical every run

  const now = Date.parse('2026-07-09T12:00:00Z')
  const DAY = 86_400_000

  const pick = (arr) => arr[Math.floor(rng() * arr.length)]

  // Tags keyed by name → { name, description }. Counts are derived from links.
  const tags = new Map()
  for (const [name, description] of TAG_DEFS) {
    tags.set(name, { name, description })
  }
  const tagNames = [...tags.keys()]

  const memories = []
  const COUNT = 250
  for (let i = 0; i < COUNT; i++) {
    const id = i + 1
    // Spread over the last ~60 days (older as id grows), plus intraday jitter.
    const daysAgo = Math.floor((i / COUNT) * 60) + rng() * 1.5
    const ts = new Date(now - daysAgo * DAY)
    const timestamp = ts.toISOString().replace('T', ' ').replace(/\.\d+Z$/, '+00:00')

    const stem = CONTENT_STEMS[i % CONTENT_STEMS.length]
    const content = `${stem} (#${id}).`

    // 1–4 distinct tags per memory.
    const nTags = 1 + Math.floor(rng() * 4)
    const memTags = new Set()
    while (memTags.size < nTags) memTags.add(pick(tagNames))

    memories.push({
      id,
      timestamp,
      agent: pick(AGENTS),
      project: pick(PROJECTS),
      content,
      type: pick(TYPES),
      tags: [...memTags].sort(),
    })
  }

  return { memories, tags }
}

/** Live seed instance the handlers mutate (merge/detach/patch/delete). */
export const db = buildSeed()

/** Recompute a tag's link count on demand. */
export function tagCount(db, name) {
  return db.memories.reduce((n, m) => n + (m.tags.includes(name) ? 1 : 0), 0)
}
