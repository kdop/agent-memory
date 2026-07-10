import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { Quasar, Notify, Dialog, Dark } from 'quasar'

// Quasar styling (fonts + icons + core css). These are the only global CSS imports.
import '@quasar/extras/roboto-font/roboto-font.css'
import '@quasar/extras/material-icons/material-icons.css'
import 'quasar/src/css/index.sass'

import App from './App.vue'
import router from './router'

async function bootstrap() {
  // Dev-only mock backend. MSW intercepts the same-origin API calls made by
  // src/api/client.js so the whole UI runs without the real FastAPI server.
  if (import.meta.env.DEV) {
    const { worker } = await import('./mocks/browser')
    await worker.start({ onUnhandledRequest: 'bypass' })
  }

  // Restore the saved theme preference ('true'/'false'); default to dark.
  const saved = localStorage.getItem('mem-dark')
  const initialDark = saved === null ? true : saved === 'true'

  const app = createApp(App)
  app.use(createPinia())
  app.use(router)
  app.use(Quasar, { plugins: { Notify, Dialog, Dark }, config: { dark: initialDark } })
  app.mount('#app')
}

bootstrap()
