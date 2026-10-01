import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'

// The policy nginx.conf serves, minus frame-ancestors (ignored in <meta>) and
// the retired Azure API host; 'self' covers the same-origin API and /ws.
const PRODUCTION_CSP = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "form-action 'self' https://accounts.google.com",
  "script-src 'self' https://maps.googleapis.com https://maps.gstatic.com",
  "style-src 'self'",
  "style-src-attr 'unsafe-inline'",
  "img-src 'self' data: blob: https://maps.googleapis.com https://maps.gstatic.com https://streetviewpixels-pa.googleapis.com",
  "font-src 'self' data:",
  "media-src 'self' blob: https:",
  "worker-src 'self' blob:",
  "connect-src 'self' https://maps.googleapis.com https://*.googleapis.com https://eventgw.twilio.com wss://voice-js.roaming.twilio.com https://media.twiliocdn.com https://sdk.twilio.com",
  'upgrade-insecure-requests',
].join('; ')

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
      // On App Platform the SPA is a static site, so nginx.conf's response
      // headers never apply and the production SPA shipped with no CSP at all
      // (security review WEB-3). A <meta> policy restores the XSS controls
      // (frame-ancestors cannot be set this way — see the review). Build only:
      // the dev server's React-refresh preamble is an inline script.
      name: 'neoh-production-csp',
      apply: 'build',
      transformIndexHtml(html) {
        // Allow exactly the inline scripts this file ships (the pre-paint
        // theme stamp) by hash — never 'unsafe-inline'.
        const hashes = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
          .map(([, body]) => `'sha256-${createHash('sha256').update(body).digest('base64')}'`)
        const csp = PRODUCTION_CSP.replace(
          "script-src 'self'",
          ["script-src 'self'", ...hashes].join(' '),
        )
        return html.replace(
          '<meta charset="UTF-8" />',
          `<meta charset="UTF-8" />\n    <meta http-equiv="Content-Security-Policy" content="${csp}" />`,
        )
      },
    },
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
