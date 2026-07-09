<script setup>
// App shell: top bar + two-pane body (main content | right rail) + login gate.
// D3–D8 render their UI into the router-view page and the named right-rail slot.
import { useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import LoginDialog from '@/components/LoginDialog.vue'

const auth = useAuthStore()
const route = useRoute()
</script>

<template>
  <q-layout view="hHh lpR fFf">
    <!-- ===================== Top bar ===================== -->
    <q-header elevated>
      <q-toolbar>
        <q-toolbar-title>Agent Memory</q-toolbar-title>

        <!-- Nav -->
        <q-btn flat no-caps label="Memories" :to="{ name: 'memories' }"
               :class="{ 'text-weight-bold': route.name === 'memories' }" />
        <q-btn flat no-caps label="Tags" :to="{ name: 'tags' }"
               :class="{ 'text-weight-bold': route.name === 'tags' }" />

        <q-space />

        <!-- Auth state / logout -->
        <div class="row items-center q-gutter-sm">
          <q-badge v-if="auth.isAuthed" color="positive" label="authed" />
          <q-btn v-if="auth.isAuthed" flat dense round icon="logout"
                 aria-label="Log out" @click="auth.logout()" />
        </div>
      </q-toolbar>
    </q-header>

    <!-- ===================== Right rail =====================
      Named slot for the tag filter (D5) and any per-page rail content.
      Pages fill it with <template #rail> … </template>. -->
    <q-drawer side="right" show-if-above bordered :width="300">
      <div class="q-pa-md">
        <!-- Pages inject rail content here via a teleport target id. -->
        <div id="right-rail-target">
          <div class="text-caption text-grey">
            Right rail — D5 tag filter mounts here (see MemoriesPage).
          </div>
        </div>
      </div>
    </q-drawer>

    <!-- ===================== Main content ===================== -->
    <q-page-container>
      <router-view />
    </q-page-container>

    <!-- ===================== Login gate =====================
      Overlays every page while unauthenticated. -->
    <LoginDialog />
  </q-layout>
</template>
