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

## How do I deploy Neoh?

**One path: DigitalOcean App Platform, through CI.** Read
[`docs/deploy-digitalocean.md`](docs/deploy-digitalocean.md). Every release
is built once by the `release` job, proven on `neoh-staging`, then promoted
unchanged to `neoh` by the `promote` job. The spec is
`infra/digitalocean/app.yaml`, rendered per environment by
`scripts/render-app-spec.py`.

Is it deployed right now, and is it ready for customers? Run
`python3 scripts/neoh-launch-readiness.py --env production` (and
`--env staging`). The current state is in
[`docs/launch-state.md`](docs/launch-state.md).

Anything AWS, Azure or Terraform in this repository is **legacy** and does
not deploy Neoh. [`docs/infrastructure-status.md`](docs/infrastructure-status.md)
classifies every infrastructure file.

Operating Neoh (incidents, onboarding a brokerage, support) starts at
[`docs/README.md`](docs/README.md).

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
| `infra/digitalocean/` | **The production deploy**: App Platform spec, smoke test, callback routes |
| `infra/` (everything else) | Legacy AWS/Azure/Terraform and GPU worker images — see `docs/infrastructure-status.md` |
| `scripts/` | Operator and QA scripts, not application code |
| `docs/` | Runbooks and setup guides — start at `docs/README.md` |
| `observability-platform/`, `legal_sentinel/`, `training_data/` | Adjacent subprojects |
