import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'

// What GET /health says, polled by the layout: the review model's state and,
// while a catch-up runs, its progress. The poll is fast (3 s) while a
// catch-up runs and slow (30 s) otherwise. `ended` goes up by one each time
// a catch-up ends, so a page can watch it and reload its list once.
// `archiveDays` is how long the server keeps an archived memory (0: for
// good; null until the first answer), for the Archived page's days left.
const FAST_MS = 3000
const SLOW_MS = 30000

export const useHealthStore = defineStore('health', () => {
  const reviewModel = ref(null) // `reachable`, `unreachable`, `off`, or null
  const catchUp = ref(null)     // { total, done } while a catch-up runs, else null
  const ended = ref(0)
  const archiveDays = ref(null)
  let timer = null

  async function check() {
    clearTimeout(timer)
    const wasRunning = catchUp.value !== null
    try {
      const h = await api.health()
      reviewModel.value = typeof h?.review_model === 'string' ? h.review_model : null
      const c = h?.catch_up
      catchUp.value = c && typeof c.total === 'number' ? { total: c.total, done: c.done ?? 0 } : null
      archiveDays.value = typeof h?.archive_days === 'number' ? h.archive_days : null
    } catch {
      reviewModel.value = null
      catchUp.value = null
    }
    if (wasRunning && catchUp.value === null) ended.value += 1
    clearTimeout(timer) // another check may have set one meanwhile
    timer = setTimeout(check, catchUp.value ? FAST_MS : SLOW_MS)
  }

  function stop() {
    clearTimeout(timer)
    timer = null
  }

  return { reviewModel, catchUp, ended, archiveDays, check, stop }
})
