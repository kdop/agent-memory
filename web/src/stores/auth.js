import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '@/api/client'

const STORAGE_KEY = 'agent-memory.token'

// Bearer-token auth. Token persists in localStorage; login validates it by
// hitting an authed endpoint (GET /tags) — a 401 means the token is bad.
export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem(STORAGE_KEY) || '')

  const isAuthed = computed(() => !!token.value)

  /**
   * Validate + store a token. Resolves on success, rejects on a bad token.
   * @param {string} candidate
   */
  async function login(candidate) {
    const trimmed = (candidate || '').trim()
    if (!trimmed) throw new Error('Token is required')
    // Set first so the client sends it on the validation call…
    token.value = trimmed
    try {
      await api.listTags() // any authed endpoint; 401 → reject
      localStorage.setItem(STORAGE_KEY, trimmed)
      return true
    } catch (err) {
      // …then roll back if the server rejected it.
      token.value = ''
      localStorage.removeItem(STORAGE_KEY)
      throw err
    }
  }

  function logout() {
    token.value = ''
    localStorage.removeItem(STORAGE_KEY)
  }

  return { token, isAuthed, login, logout }
})
