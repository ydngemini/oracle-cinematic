# Oracle / Neoh

Autonomous real-estate command center. Two things are deployed, and a third
directory at the root looks like a third one but is not.

## What actually runs

| Path | Stack | Built by |
|---|---|---|
| `backend/` | FastAPI + asyncpg + Postgres (RLS-enforced multi-tenancy) | `backend/Dockerfile` |
| `oracle-app/` | Vite + React, raw WebGL, CSS Modules | `oracle-app/Dockerfile` |

`docker-compose.yml` brings both up with Postgres. CI (`.github/workflows/ci.yml`)
runs the backend pytest suite and the frontend lint + unit tests + build.

```bash
# local stack
./scripts/dev-start.sh

# backend tests
cd backend && python -m pytest tests -q

# frontend
cd oracle-app && npm ci && npm test && npm run build
```

## What does not run: `src/` (`oracle-cinematic`)

The root `package.json`, `next.config.ts`, `tsconfig.json`, `postcss.config.mjs`,
`eslint.config.mjs` and `src/` belong to a **Next.js prototype that no
Dockerfile and no compose service builds.** It is kept for its cinematic /
reel / tour experiments; the shipping equivalents live in `oracle-app/`, which
serves its own `/reel` route.

It is deliberately not wired into CI. If you are looking for the app, it is
`oracle-app/`. Treat anything in `src/` as reference material until someone
decides to either revive or remove it. A few `scripts/*.py` Playwright helpers
still drive it and are in the same state.

A root `server.py` used to sit alongside these — a mock WebSocket "AI swarm"
demo with `allow_origins=["*"]`, superseded by `backend/server.py` and removed
because `python server.py` in the repo root started it.

## Directory map

| Path | What it is |
|---|---|
| `backend/` | The API. `server.py` is the FastAPI app; routers are one module per surface |
| `backend/db/migrations/` | Numbered SQL migrations — read its `README.md` before adding one |
| `oracle-app/` | The shipping frontend |
| `infra/` | Azure deployment, reconstruction workers, Terraform |
| `scripts/` | Operator and QA scripts, not application code |
| `docs/` | Runbooks and setup guides |
| `observability-platform/`, `legal_sentinel/`, `training_data/` | Adjacent subprojects |
