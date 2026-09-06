import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  // Read env from the project root (one .env for backend + frontend). Vite only
  // exposes VITE_-prefixed vars to the client, so the Stripe/AWS secrets in the
  // root .env are never bundled. Without this, VITE_WS_URL / VITE_TENANT_ID /
  // VITE_BILLING_BYPASS were silently ignored and the app ran on hard defaults.
  // NOTE: in the Docker dev container only oracle-app/ is mounted, so '..' has
  // no .env — VITE_* values must arrive via compose `environment:` there.
  envDir: '..',
  // Serve the API from the SPA's own origin. The browser used to call an
  // absolute http://localhost:8000, which only works when the backend's port
  // is published on a host the browser can reach — under Docker-in-Docker it
  // is not, and every request died as ERR_CONNECTION_RESET. Proxying keeps the
  // app on one origin, so it no longer depends on how the backend is published.
  // Same lesson the WebSocket URL already learned: derive from the page.
  server: {
    host: true,
    proxy: Object.fromEntries(
      ['/api', '/auth', '/ws'].map((path) => [path, {
        target: process.env.ORACLE_PROXY_TARGET || 'http://backend:8000',
        changeOrigin: true,
        ws: path === '/ws',
      }]),
    ),
  },
  plugins: [
    react(),
    {
      name: 'bundle-neoh-service-worker',
      apply: 'build',
      buildStart() {
        this.emitFile({
          type: 'chunk',
          fileName: 'sw-oracle.js',
          id: fileURLToPath(new URL('./src/sw-oracle.js', import.meta.url)),
        })
      },
    },
  ],
})
