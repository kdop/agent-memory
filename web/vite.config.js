import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { quasar, transformAssetUrls } from '@quasar/vite-plugin'

// Base URL for the API is handled inside src/api/client.js (same-origin in both
// dev and prod). In dev the MSW worker intercepts those same-origin requests;
// in prod they hit the FastAPI server behind the same origin. If you ever need
// a real backend during dev instead of MSW, add a `server.proxy` block here.
export default defineConfig({
  plugins: [
    vue({ template: { transformAssetUrls } }),
    quasar({
      // Absolute path: the plugin injects `@import '<this>'` into Quasar's own
      // sass, so a relative path wouldn't resolve.
      sassVariables: fileURLToPath(new URL('./src/quasar-variables.sass', import.meta.url)),
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
})
