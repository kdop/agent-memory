<script setup>
// Landing / sign-in page. Shown whenever there is no valid token — before the
// first login and again after logout. Replaces the old modal login gate with a
// full page. On submit it validates via auth.login (GET /tags) and kicks off the
// initial data fetch.
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useMemoriesStore } from '@/stores/memories'
import { useTagsStore } from '@/stores/tags'

const auth = useAuthStore()
const memories = useMemoriesStore()
const tags = useTagsStore()
const router = useRouter()

const token = ref('')
const submitting = ref(false)
const errorMsg = ref('')

async function submit() {
  if (!token.value) return
  errorMsg.value = ''
  submitting.value = true
  try {
    await auth.login(token.value)
    token.value = ''
    // Start clean: no leftover filters/paging, unadulterated URL.
    memories.reset()
    await router.replace({ name: 'memories', query: {} })
    await Promise.all([memories.fetch(), tags.fetch()])
  } catch (err) {
    errorMsg.value = err?.status === 401 ? 'Invalid token.' : (err?.message || 'Login failed.')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <q-page class="flex flex-center column q-pa-md">
    <div class="landing-card column items-center text-center">
      <q-icon name="psychology" size="72px" color="primary" />
      <div class="text-h4 text-weight-bold q-mt-md">Agent Memory</div>
      <div class="text-subtitle1 text-grey q-mt-sm" style="max-width: 460px">
        A shared, searchable memory for your AI agents — browse, search, tag, and
        curate everything they've decided, learned, and built.
      </div>

      <q-card flat bordered class="q-mt-xl q-pa-sm" style="width: 360px; max-width: 90vw">
        <q-card-section class="q-pb-none">
          <div class="text-h6">Sign in</div>
          <div class="text-caption text-grey">Enter your API bearer token.</div>
        </q-card-section>
        <q-card-section>
          <q-input
            v-model="token"
            type="password"
            label="Bearer token"
            autofocus
            outlined
            :error="!!errorMsg"
            :error-message="errorMsg"
            @keyup.enter="submit"
          />
        </q-card-section>
        <q-card-actions>
          <q-btn
            class="full-width"
            color="primary"
            label="Sign in"
            :loading="submitting"
            :disable="!token"
            @click="submit"
          />
        </q-card-actions>
      </q-card>
    </div>
  </q-page>
</template>

<style scoped>
.landing-card {
  margin-top: 8vh;
}
</style>
