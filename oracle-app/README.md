# oracle-app — the Neoh web app

The shipping frontend: Vite + React, CSS Modules, no UI library. The product
is three places, **Home / Work / Neoh**, with people, properties, deals and
conversations as context inside them (`src/components/CrmShell.jsx`).

```bash
npm ci
npm run dev           # proxies API calls to ORACLE_PROXY_TARGET (default http://backend:8000,
                      # the compose service; ./scripts/dev-start.sh runs the whole stack)
npm test              # vitest
npm run lint
npm run build         # production bundle in dist/
npm run bundle:check  # bundle-size budget (CI enforces it)
```

Configuration is compiled in at build time (`VITE_*`). Production builds leave
`VITE_API_BASE` and `VITE_WS_URL` **empty**, so the bundle calls its own
origin. The same bundle runs on staging and production. The only public keys
are the referrer-locked Google Maps pair (`VITE_GOOGLE_MAPS_KEY`,
`VITE_GOOGLE_MAP_ID`). Never put a backend secret in a `VITE_*` variable:
it ships to every browser.

In production the bundle is served by this image's own nginx (`Dockerfile`,
`nginx.conf`), so the CSP, HSTS and frame headers apply. It is deployed as the
`web` component of `infra/digitalocean/app.yaml`. For deploys, see
[`../docs/deploy-digitalocean.md`](../docs/deploy-digitalocean.md).
