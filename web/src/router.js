import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'
import MemoriesPage from '@/pages/MemoriesPage.vue'
import TagsPage from '@/pages/TagsPage.vue'

// All app routes live under /app, deliberately namespaced away from the backend
// API's flat paths (/memories, /tags, /projects, ...). Same-origin serving means a
// client route that happened to match an API path (the old bare `/tags`) would
// work fine for in-app navigation but 401/404 on a hard refresh (F5) — the browser
// makes a real HTTP request straight to the API route, no SPA involved. Keeping
// app routes under their own prefix makes that collision structurally impossible,
// now and for any future route.
const routes = [
  {
    path: '/app',
    component: MainLayout,
    children: [
      { path: '', name: 'memories', component: MemoriesPage },
      { path: 'tags', name: 'tags', component: TagsPage },
    ],
  },
  { path: '/', redirect: { name: 'memories' } },
  // Unknown paths fall back to the memories view.
  { path: '/:pathMatch(.*)*', redirect: { name: 'memories' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
