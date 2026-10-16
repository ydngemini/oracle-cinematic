# Neoh Space — integration map (T12.01)

Read at `42f0c0a` plus the L12-03 change (branch `claude/continue-n7jgwq`, 2026-10-16). Every line is a `path:line` read in the code, with a status: **real code**, **partial**, or **none** (spec only). Line numbers drift; re-verify before relying on one (CONTRACT C1.5). Ledger: L12-03.

| # | Step | Evidence | What it does today | Status |
|---|---|---|---|---|
| 1 | Capture entry (agent) | `oracle-app/src/components/PropertiesTab.jsx` mounts `PropertyViewTab`; `PropertyViewTab.jsx` file input (`accept=image/*,video/...`, no `capture` attribute); `CaptureSessionPanel` mounted below it | Agent picks existing files; the panel shows `CAPTURE_STEPS` (`lib/tour/captureGuide.ts`) and a Build button. **Since L12-03:** "Capture with camera" opens `CaptureStudio` (lazy). | real code |
| 1b | Client capture link | `backend/property_view_api.py:918` `create_upload_link` (token `secrets.token_urlsafe(32)`, stored hashed, TTL, `max_uploads`); `_resolve_link` checks revoked/expired/quota; `GET`/`POST /public/property-upload/{token}`; page `PropertyUploadPage.jsx` at `/property-upload/` | Client uploads land as `review_status='pending'`, one request per file | real code |
| 2 | Camera | Before L12-03: no `getUserMedia`/`MediaRecorder`/worker in `oracle-app/src` except audio in `PowerDialerPage.jsx` | **L12-03:** `lib/capture/cameraSupport.js` (feature detection, error → state), `CaptureStudio.jsx` (preview, record, photo), `lib/capture/qualityWorker.js` + `hintScheduler.js` (one frame in flight, adaptive interval) | real code (jsdom-tested; no real-device run) |
| 3 | Upload | `POST /api/crm/property-view/media` (`property_view_api.py`), called per file by `PropertyViewTab.uploadEach`; video via `_put_video_to_storage`, photos via `media_storage.put_media_bytes` (`media_storage.py:45`) | Whole-file multipart, one file per request, sequential; failed files retried alone. **Not present:** chunked/resumable upload, content hashes, upload receipts, duplicate detection. **L12-03:** originals staged in IndexedDB until the endpoint acknowledges each file; a reload lists what is still waiting | partial |
| 4 | Storage, retention, deletion | `media_storage.py` keys `…/{tenant}/{kind}/{token}`; `privacy_data_map.py` classes `capture_sessions` as `SPATIAL_SOURCE`; `retention_policy.py` keeps originals until tenant erasure; `DELETE /crm/media/{id}`; `DELETE /crm/space` removes derived data only | Per-capture consent/retention tracking (spec) not present | real code |
| 5 | Worker, GPU, stages, gate | `reconstruction_worker.py:136` claims with `FOR UPDATE SKIP LOCKED`; capture gate `capture_quality.assess` before any GPU; post-build `recon_quality.assess`; stages `space_status.py`; providers `reconstruction_providers.py` (RunPod pod / serverless) | The job table is the queue; stages are labels, not percentages | real code |
| 6 | Viewer, publish, rollback | Atomic publish `reconstruction_worker.py:992-1029` (old row `superseded_at`); resolver `tour_api.py`; signed assets with Range (`space_assets.py`); viewers `TourViewer.jsx`, `PropertyTourViewer.tsx`, `lib/tour/deviceCapability.ts` | Superseded rows are kept, but no route or UI restores one | publish real; rollback partial |

## Tests per step

1. `CaptureSessionPanel.test.jsx`, `PropertyViewTab.test.jsx`, `HouseWorkspace.test.jsx`; L12-03 adds `CaptureStudio.test.jsx`, `lib/capture/capture.test.js`.
2. L12-03 tests above (jsdom with a fake camera and recorder). No real-device protocol run (AC12.02 open).
3. `test_media_upload_offload.py`, `test_object_storage.py`, `PropertyViewTab.test.jsx` (per-file retry).
4. `test_privacy_data_map.py`, `test_privacy_lifecycle_live.py`, `test_privacy_api.py`.
5. `test_neoh_space_pipeline.py`, `test_capture_pipeline.py`, `test_capture_sidecars.py`, `test_recon_quality.py`, `test_reconstruction_durable_queue.py`, `test_reconstruction_orphans.py`, `test_reconstruction_reaper.py`, `test_reconstruction_providers.py`.
6. `test_neoh_space_routes.py`, `test_tour_resolver.py`, `test_tour_tier_honesty.py`, `TourViewer.test.jsx`, `lib/tour/neohSpace.test.ts`.

## Missing, in order (T12.05 onward)

1. Server upload sessions: chunk offsets, acknowledged-byte progress, per-chunk and whole-file hashes, idempotent receipts, duplicate detection (replaces whole-file 512 MB requests).
2. Room segments and doorway marks during capture (T12.06), server preflight.
3. A restore action for a superseded space.
4. Real-device protocol (iPhone Safari, Android Chrome; three connected rooms; scripted interruptions) — AC12.02.
