import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'
import MemoriesPage from '@/pages/MemoriesPage.vue'
import TagsPage from '@/pages/TagsPage.vue'

// Both pages render inside MainLayout, which also hosts the login gate so an
// unauthenticated user sees the token dialog over whichever route they landed on.
const routes = [
  {
    path: '/',
    component: MainLayout,
    children: [
      { path: '', name: 'memories', component: MemoriesPage },
      { path: 'tags', name: 'tags', component: TagsPage },
    ],
  },
  // Unknown paths fall back to the memories view.
  { path: '/:pathMatch(.*)*', redirect: { name: 'memories' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
