# TODO — Oracle / Neoh

Auto-generated from the current state of `feat/azure-platform-and-review-fixes`
(112 modified/untracked files, 6 failing tests). Check items off as they land.

## Blocker: failing tests

- [ ] Fix `backend/tests/test_oncompute_provider.py` (6 failures). The token-mode
      fixtures set `ONCOMPUTE_AUTH_TOKEN` but never clear
      `ONCOMPUTE_PRIVATE_KEY_FILE` / `ONCOMPUTE_PRIVATE_KEY`, so they read both
      credentials once `.env` is loaded (via `server.py` `load_dotenv`) and hit
      the "not both" guard. Add `monkeypatch.delenv(..., raising=False)` for both
      key vars in the `configured` fixture (mirror the `key_configured` fixture,
      which already deletes the token side).

## Persist the branch (uncommitted work)

The working tree has a large mixed changeset that should be committed in coherent
chunks, not one mega-commit.

- [ ] Commit the staged deletions (`server.py`, `spatial_agent.py`,
      `fema_flood.py`, `CaptureWizard*`, `DashboardLayout*`, `MediaUploader*`,
      `PropertyCanvas*`). Verified: only docstring/comment references remain, no
      live imports are broken.
- [ ] Commit new modules + tests currently untracked:
      - [ ] `backend/tenant_engines.py` (per-tenant ref-counted `WorkflowEngine`)
      - [ ] `backend/decision_traces.py` (AI-decision training corpus)
      - [ ] `backend/db/migrations/0074_ai_decision_traces.sql`
      - [ ] `backend/tests/test_decision_traces.py`, `test_model_registry_lifecycle.py`,
        `test_oncompute_provider.py`, `test_pool_arithmetic.py`,
        `test_rate_limit_buckets.py`, `test_ws_session_shape.py`
- [ ] Commit migrations `0068`–`0073` (media FK, agency/law, compliance checklist
      writes, media provenance, pano scenes, capture sessions) + their
      `migrations/README.md`.

## Migrations

- [ ] Ensure `0074_ai_decision_traces.sql` is applied before `decision_traces.py`
      runs against a live DB — `SURFACE_*` constants are keyed to the CHECK
      constraint; a missing value fails at insert.
- [ ] Confirm `0068`–`0074` run clean through `backend/run_migrations.py`
      (lexicographic order; never renumber an already-applied file).

## Frontend

- [ ] Run `cd oracle-app && npm test` — new `PanoViewer`, `CaptureSessionPanel`,
      `panoGraph.ts`, `HouseWorkspace.test.jsx` are unverified.
- [ ] Run `npm run build` and `scripts/check-bundle-budget.mjs` (new bundle
      budget gate; `bundle-budget.json` added).

## Deferred decision

- [ ] Resolve the `src/` (`oracle-cinematic`) Next.js prototype and root
      `package.json`/`next.config.ts` scaffolding: revive or remove. README marks
      it as reference-only and deliberately out of CI; item 29 of the
      implementation matrix still cites `src/components/three/` and `tour/`.
- [ ] Clean stale comment/docstring references to deleted modules
      (`spatial_agent`, `CaptureWizard`, `DashboardLayout`, `MediaUploader`,
      `PropertyCanvas`) in `property_view_api.py`, `reconstruction_*.py`,
      `tour_generator.py`, `video_studio.py`, `workflow_engine.py`, and
      `oracle-app/src/components/*`.
