<script setup>
// App shell: top bar + two-pane body (main content | right rail) + login gate.
// Pages and components render their UI into the router-view page and the named right-rail slot.
// The health banner under the top bar shows the review model's state when the
// server reports one (`review_model` on GET /health). While a catch-up runs,
// a thin progress bar under the top bar shows how far it got (`catch_up`).
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuasar } from 'quasar'
import { useAuthStore } from '@/stores/auth'
import { useMemoriesStore } from '@/stores/memories'
import { useHealthStore } from '@/stores/health'
import Landing from '@/components/Landing.vue'

const auth = useAuthStore()
const memories = useMemoriesStore()
const route = useRoute()
const router = useRouter()
const $q = useQuasar()
const health = useHealthStore()

// `reachable`, `unreachable`, `off`, or null when the server does not say.
const reviewModel = computed(() => health.reviewModel)
const REVIEW_MODEL_COLOR = { reachable: 'positive', unreachable: 'warning', off: 'grey-7' }
const REVIEW_MODEL_TEXT = {
  reachable: 'Review model: reachable. New memories get a verdict.',
  unreachable: 'Review model: unreachable. New memories stay unverified until it is back.',
  off: 'Review model: off. Memories are not reviewed.',
}

// The catch-up's progress, for the bar: "10 of 100 done, 90 remaining".
const catchUp = computed(() => health.catchUp)
const catchUpText = computed(() => {
  const c = catchUp.value
  if (!c) return ''
  return `Reviewing memories: ${c.done} of ${c.total} done, ${c.total - c.done} remaining`
})

onMounted(() => health.check())
onUnmounted(() => health.stop())

function toggleDark() {
  $q.dark.toggle()
  localStorage.setItem('mem-dark', String($q.dark.isActive))
}

// Clear all filters/paging, land on the clean memories URL, and reload the list.
async function goHome() {
  memories.reset()
  await router.push({ name: 'memories', query: {} }).catch(() => {})
  memories.fetch()
}

function onLogout() {
  auth.logout()
  goHome()
}
</script>

<template>
  <q-layout view="hHh lpR fFf">
    <!-- ===================== Top bar ===================== -->
    <q-header elevated>
      <q-toolbar>
        <q-btn flat no-caps dense
               label="Agent Memory" class="text-h6 q-px-sm"
               @click="goHome" />

        <!-- Nav (only once signed in) -->
        <template v-if="auth.isAuthed">
          <q-btn flat no-caps label="Memories" :to="{ name: 'memories' }"
                 :class="{ 'text-weight-bold': route.name === 'memories' }" />
          <q-btn flat no-caps label="Tags" :to="{ name: 'tags' }"
                 :class="{ 'text-weight-bold': route.name === 'tags' }" />
          <q-btn flat no-caps label="Flagged" :to="{ name: 'flagged' }"
                 :class="{ 'text-weight-bold': route.name === 'flagged' }" />
          <q-btn flat no-caps label="Archived" :to="{ name: 'archived' }"
                 :class="{ 'text-weight-bold': route.name === 'archived' }" />
        </template>

        <q-space />

        <!-- Theme toggle + auth state / logout -->
        <div class="row items-center q-gutter-sm">
          <!-- health: the review model, when the server reports it -->
          <q-badge v-if="reviewModel" :color="REVIEW_MODEL_COLOR[reviewModel] || 'grey-7'"
                   class="health-badge" data-test="review-model">
            <q-icon name="psychology" size="xs" class="q-mr-xs" />
            review model: {{ reviewModel }}
            <q-tooltip>{{ REVIEW_MODEL_TEXT[reviewModel] || `Review model: ${reviewModel}` }}</q-tooltip>
          </q-badge>
          <q-btn flat dense round
                 :icon="$q.dark.isActive ? 'light_mode' : 'dark_mode'"
                 :aria-label="$q.dark.isActive ? 'Switch to light theme' : 'Switch to dark theme'"
                 @click="toggleDark" />
          <q-badge v-if="auth.isAuthed" color="positive" label="authed" />
          <q-btn v-if="auth.isAuthed" flat dense round icon="logout"
                 aria-label="Log out" @click="onLogout" />
        </div>
      </q-toolbar>
    </q-header>

    <!-- ===================== Right rail (only when signed in) =====================
      Pages inject rail content (TagFilter) via the teleport target id. -->
    <q-drawer v-if="auth.isAuthed" side="right" show-if-above bordered :width="300">
      <div class="q-pa-md">
        <div id="right-rail-target"></div>
      </div>
    </q-drawer>

    <!-- ===================== Main content ===================== -->
    <q-page-container>
      <!-- Catch-up progress: only while a catch-up runs. -->
      <div v-if="auth.isAuthed && catchUp" class="q-px-md q-pt-sm" data-test="catch-up-progress">
        <div class="text-caption">{{ catchUpText }}</div>
        <q-linear-progress :value="catchUp.total ? catchUp.done / catchUp.total : 0"
                           color="primary" rounded size="6px" />
      </div>
      <!-- Health banner: only when the review model is on but not answering. -->
      <q-banner v-if="auth.isAuthed && reviewModel === 'unreachable'" dense
                class="bg-orange-1 text-orange-9 q-mx-md q-mt-md rounded-borders">
        <template #avatar><q-icon name="warning" /></template>
        {{ REVIEW_MODEL_TEXT.unreachable }}
      </q-banner>
      <!-- Signed in → the app; otherwise the landing / sign-in page. -->
      <router-view v-if="auth.isAuthed" />
      <Landing v-else />
    </q-page-container>
  </q-layout>
</template>
