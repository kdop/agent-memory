<script setup>
// Login gate. Shows a modal token field whenever there is no valid token, over
// whatever page is mounted. On submit it validates via auth.login (GET /tags),
// and on success triggers the initial data fetch for the current page.
import { ref, computed } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useMemoriesStore } from '@/stores/memories'
import { useTagsStore } from '@/stores/tags'

const auth = useAuthStore()
const memories = useMemoriesStore()
const tags = useTagsStore()

const token = ref('')
const submitting = ref(false)
const errorMsg = ref('')

// Persistent (non-dismissable) while unauthenticated.
const open = computed(() => !auth.isAuthed)

async function submit() {
  errorMsg.value = ''
  submitting.value = true
  try {
    await auth.login(token.value)
    token.value = ''
    // Pages skip fetching while unauthed; kick them off now.
    await Promise.all([memories.fetch(), tags.fetch()])
  } catch (err) {
    errorMsg.value = err?.status === 401
      ? 'Invalid token.'
      : (err?.message || 'Login failed.')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <q-dialog :model-value="open" persistent>
    <q-card style="min-width: 340px">
      <q-card-section>
        <div class="text-h6">Sign in</div>
        <div class="text-caption text-grey">
          Enter your API bearer token to access the dashboard.
        </div>
      </q-card-section>

      <q-card-section>
        <q-input
          v-model="token"
          type="password"
          label="Bearer token"
          autofocus
          :error="!!errorMsg"
          :error-message="errorMsg"
          @keyup.enter="submit"
        />
      </q-card-section>

      <q-card-actions align="right">
        <q-btn
          color="primary"
          label="Sign in"
          :loading="submitting"
          :disable="!token"
          @click="submit"
        />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>
