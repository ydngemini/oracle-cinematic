import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig(({ command }) => ({
  // Read env from the project root (one .env for backend + frontend). Vite only
  // exposes VITE_-prefixed vars to the client, so the Stripe/AWS secrets in the
  // root .env are never bundled. Without this, VITE_WS_URL / VITE_TENANT_ID /
  // VITE_BILLING_BYPASS were silently ignored and the app ran on hard defaults.
  // NOTE: in the Docker dev container only oracle-app/ is mounted, so '..' has
  // no .env — VITE_* values must arrive via compose `environment:` there.
  envDir: '..',
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      injectRegister: 'auto',
      strategies: 'injectManifest',
      srcDir: 'src',
      filename: 'sw-oracle.js',

      // Dev: serve a self-destroying stub instead of sw-oracle.js. The SW's
      // CacheFirst script route is correct for hashed production assets, but
      // dev module URLs (/src/...) never change, so an installed SW pins the
      // first copy it ever cached — stale code survives edits, env changes,
      // and container restarts, and the HMR websocket breaks. The stub also
      // unregisters any previously-installed SW from visiting browsers.
      selfDestroying: command === 'serve',

      injectManifest: {
        maximumFileSizeToCacheInBytes: 100 * 1024 * 1024,
        globPatterns: [
          '**/*.{js,css,html,woff2,png,svg,ico}',
        ],
      },

      manifest: {
        name: 'Oracle Agent CRM',
        short_name: 'Oracle',
        description: 'AI-run real-estate agent command center — listings, clients, comms',
        theme_color: '#0a0a0c',
        background_color: '#000000',
        display: 'standalone',
        // Portrait: the 5-tab Deck CRM is a one-hand phone app (was landscape
        // for the desktop HUD era).
        orientation: 'portrait',
        icons: [
          {
            src: '/oracle-192.png',
            sizes: '192x192',
            type: 'image/png',
          },
          {
            src: '/oracle-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'any maskable',
          },
        ],
      },

      devOptions: {
        enabled: true,
        type: 'module',
      },
    }),
  ],
}))
