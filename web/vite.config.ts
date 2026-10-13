import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// The backend serves the built bundle from `/`, so `base` stays the default.
// In development the Vite dev server proxies the API to the running backend,
// which keeps `npm run dev` usable without rebuilding after every change.
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.PAPEROPENCLAW_API || 'http://127.0.0.1:8787',
        changeOrigin: true,
        // SSE must not be buffered by the dev proxy.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (String(proxyRes.headers['content-type'] || '').includes('text/event-stream')) {
              proxyRes.headers['x-accel-buffering'] = 'no'
              proxyRes.headers['cache-control'] = 'no-cache'
            }
          })
        },
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
})
