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
