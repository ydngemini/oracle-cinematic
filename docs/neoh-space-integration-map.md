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

## Quality labels: consumer audit (T12.10)

Read at `22a7f58` (2026-10-17), with the certification change. The question: does any reader infer quality from a space existing or from a non-blocked gate verdict? Each one is now bound to the certification state (`backend/space_certification.py`, see [neoh-space-quality.md, "Certification"](neoh-space-quality.md#certification-t1209)) or relabelled. Line numbers are for the change; re-verify before relying on one.

| Consumer | Before | Now | Test |
|---|---|---|---|
| Tour badge, label, note (`tour_api._SPLAT_BADGE` :62, `build_tour` :372) | state from caveat keys only; `[]` gave `measured` "3D Walkthrough"; every capture's note said "photoreal … the actual home" | badge, label and note from one table keyed by the certification; only `verified` says "Full 3D Walkthrough" or "photoreal"; per-asset `certification`, top-level `splat_certification`, `splat_caveats` = caveats + reasons | `test_tour_tier_honesty.py`, `test_space_certification_consumers.py` (AC12.06 across every case) |
| Evidence read (`tour_api.fetch_tour_rows` :321, `_EVIDENCE_COLUMNS` :307) | the newest job's `limitations` only; no freshness | one lateral read of the build's caveats, measurements and input set, and a derived `evidence_fresh` | `test_space_certification_live.py` (real PostgreSQL, FORCE RLS) |
| `walkable` / `walkable_interior` (`build_tour` :468) | read by callers as if it meant a usable interior | kept as navigability; `whole_home_verified` (:473, :559) is the quality claim, true only for `verified` | `test_space_certification_consumers.py` |
| Property workspace (`HouseWorkspace.jsx` :535), Property View (`PropertyViewTab.jsx` :885), dossier (`DossierPanel.jsx` :425) | viewer opened with no label: a bare "3D walkthrough", no caveats | pass `splat_label` and `splat_caveats` | `HouseWorkspace.test.jsx`, `PropertyViewTab.test.jsx`, `TourViewer.test.jsx` (source guard over all five call sites) |
| Neoh entity sheet (`EntitySheet.jsx` :121) | already bound (the one call site that was) | unchanged; now pinned | `EntitySheet.test.jsx` |
| Viewer fallback (`TourViewer.jsx` :86, :265) | missing label → "3D walkthrough" | fails closed: "3D walkthrough · quality not verified" | `TourViewer.test.jsx` |
| Homeowner portal (`client_portal.py` :813; `SecureDossierPage.jsx` :329, :348) | no label, state or caveats reached the client | `splat_quality_state`, `splat_label`, `splat_caveats` (plain words; no check detail, no job id); the label is shown before "Step inside" and passed to the viewer | `test_space_certification_consumers.py`, `SecureDossierPage.test.jsx` |
| Job view and operator view (`space_status.public_view` :241, `operator_view` :250; `tour_api._job_payload` :808) | READY was the only state | `certification` beside the stage; a build view never reads freshness, so it is never `verified`, and it does not say freshness "can't be told"; operators get each check, its values and lines, here and on `GET /crm/space` | `test_space_certification_consumers.py` |
| Capture panel (`CaptureSessionPanel.jsx` :50, :280) | "Your space is ready." and caveats only | a separate "Quality check" line with the reasons; once ready it re-reads `GET /crm/space` and shows the published space's certification (which reads freshness); operators see each check | `CaptureSessionPanel.test.jsx` |
| Golden-capture proof script (`scripts/golden_capture.py`, `closing_note`) | "the tour should offer 'Full 3D'" after every successful run | says the tour opens the 3D walkthrough labelled with its quality state, and prints the build's certification label and reasons | `test_space_certification_consumers.py` |
| AI: `get_property_tour` (`ai_tools_read.py` :2144, :2172) | label and `walkable` only | per-capture `quality_state`, `caveats`, `whole_home_verified`; the note says `walkable` is navigability and never to call a space verified, professional, full or photoreal unless `quality_state` is verified | `test_space_certification_consumers.py` |
| AI: `share_property_tour` (`ai_tools_gated.py` :786) | told the model the link shows "the 3D tour" | `quality_label` in the result and on the approval's draft payload; whether an unverified space may be shared stays an owner decision (L12-01) | `test_space_certification_consumers.py` |
| Tour header badge (`PropertyTour.jsx` :549) | showed `badge` with `honest_note` as tooltip | no change: both are now bound server-side | backend tests above |
| Phone-scan ingest (`property_view_api.py`) | a captured splat with no build | no change: no evidence → `pending`, never certifiable | `test_space_certification.py`, live test |
| Public sites (`public_sites_api.py`, `sites_api.py`, `PublicSite.jsx`, `SitePreview.jsx`) | read no tour, splat or media data | not applicable | — |
| `tourOffer.ts` and the "Step inside · walk the 3D space" buttons | action labels, no quality adjective | no change | — |
| `SPLAT_READY` feed (`useOracleWebSocket.js`) | "3D reconstruction is live", tag "SCAN LIVE": no quality adjective, but C12 reserves "live" | not changed in this slice (no test covers the hook); reword to "published" when that file is next touched | — |

The build records its input set for the freshness check: `reconstruction_worker.capture_input_sql` (:400), shared with the tour's read, and `diagnostics.capture.input_media_ids` / `skipped_media_ids` / `newest_input_at` (:1245). That is one write, after the capture gate, and it is required: if it fails, the build stops before any GPU and publishes nothing. Capture input is `provenance = 'captured'` and `review_status = 'approved'` only, so a generated listing reel (video studio) or an unreviewed or rejected client-link upload neither reaches a build nor makes a space stale. A skipped row (unreadable, a video with no frames, bytes the gate cannot decode, or a row none of whose frames fell inside the gate's 900-frame sample) stays in the input set and is counted by the tour as `evidence_unread`, which keeps the space from `verified`.

## Tests per step

1. `CaptureSessionPanel.test.jsx`, `PropertyViewTab.test.jsx`, `HouseWorkspace.test.jsx`; L12-03 adds `CaptureStudio.test.jsx`, `lib/capture/capture.test.js`.
2. L12-03 tests above (jsdom with a fake camera and recorder). No real-device protocol run (AC12.02 open).
3. `test_media_upload_offload.py`, `test_object_storage.py`, `PropertyViewTab.test.jsx` (per-file retry).
4. `test_privacy_data_map.py`, `test_privacy_lifecycle_live.py`, `test_privacy_api.py`.
5. `test_neoh_space_pipeline.py`, `test_capture_pipeline.py`, `test_capture_sidecars.py`, `test_recon_quality.py`, `test_reconstruction_durable_queue.py`, `test_reconstruction_orphans.py`, `test_reconstruction_reaper.py`, `test_reconstruction_providers.py`.
6. `test_neoh_space_routes.py`, `test_tour_resolver.py`, `test_tour_tier_honesty.py`, `TourViewer.test.jsx`, `lib/tour/neohSpace.test.ts`; certification: `test_space_certification.py`, `test_space_certification_consumers.py`, `test_space_certification_live.py`.

## Missing, in order (T12.05 onward)

1. Server upload sessions: chunk offsets, acknowledged-byte progress, per-chunk and whole-file hashes, idempotent receipts, duplicate detection (replaces whole-file 512 MB requests).
2. Room segments and doorway marks during capture (T12.06), server preflight.
3. A restore action for a superseded space.
4. Real-device protocol (iPhone Safari, Android Chrome; three connected rooms; scripted interruptions) — AC12.02.
