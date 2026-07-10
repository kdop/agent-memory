<script setup>
// App shell: top bar + two-pane body (main content | right rail) + login gate.
// D3–D8 render their UI into the router-view page and the named right-rail slot.
import { useRoute, useRouter } from 'vue-router'
import { useQuasar } from 'quasar'
import { useAuthStore } from '@/stores/auth'
import { useMemoriesStore } from '@/stores/memories'
import Landing from '@/components/Landing.vue'

const auth = useAuthStore()
const memories = useMemoriesStore()
const route = useRoute()
const router = useRouter()
const $q = useQuasar()

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
               label="Agent Memorys" class="text-h6 q-px-sm"
               @click="goHome" />

        <!-- Nav (only once signed in) -->
        <template v-if="auth.isAuthed">
          <q-btn flat no-caps label="Memories" :to="{ name: 'memories' }"
                 :class="{ 'text-weight-bold': route.name === 'memories' }" />
          <q-btn flat no-caps label="Tags" :to="{ name: 'tags' }"
                 :class="{ 'text-weight-bold': route.name === 'tags' }" />
        </template>

        <q-space />

        <!-- Theme toggle + auth state / logout -->
        <div class="row items-center q-gutter-sm">
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
      Pages inject rail content (D5 TagFilter) via the teleport target id. -->
    <q-drawer v-if="auth.isAuthed" side="right" show-if-above bordered :width="300">
      <div class="q-pa-md">
        <div id="right-rail-target"></div>
      </div>
    </q-drawer>

    <!-- ===================== Main content ===================== -->
    <q-page-container>
      <!-- Signed in → the app; otherwise the landing / sign-in page. -->
      <router-view v-if="auth.isAuthed" />
      <Landing v-else />
    </q-page-container>
  </q-layout>
</template>
