import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  // Read env from the project root (one .env for backend + frontend). Vite only
  // exposes VITE_-prefixed vars to the client, so the Stripe/AWS secrets in the
  // root .env are never bundled. Without this, VITE_WS_URL / VITE_TENANT_ID /
  // VITE_BILLING_BYPASS were silently ignored and the app ran on hard defaults.
  envDir: '..',
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      injectRegister: 'auto',
      strategies: 'injectManifest',
      srcDir: 'src',
      filename: 'sw-oracle.js',

      injectManifest: {
        maximumFileSizeToCacheInBytes: 100 * 1024 * 1024,
        globPatterns: [
          '**/*.{js,css,html,woff2,png,svg,ico}',
        ],
      },

      manifest: {
        name: 'Oracle Command Center',
        short_name: 'Oracle',
        description: 'Autonomous real-estate acquisition command center',
        theme_color: '#0a0a0c',
        background_color: '#000000',
        display: 'standalone',
        orientation: 'landscape',
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
})
