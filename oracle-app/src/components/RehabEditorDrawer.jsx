import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Clock, ImageUp, Loader2, MapPinned, Save, Wand2, X } from 'lucide-react';
import { crmGet, crmPost, crmPut, crmUpload } from '../state/useCrmApi';
import { useFloorplanEditor } from '../lib/floorplan/useFloorplanEditor';
import { EMPTY_FLOORPLAN } from '../lib/floorplan/protocol';
import { useRehabCalculator } from '../hooks/useRehabCalculator';
import { FloorplanCanvas } from './FloorplanCanvas';
import styles from './RehabEditorDrawer.module.css';

/**
 * RehabEditorDrawer — the floor-plan editor wired to live rehab costing,
 * mounted from the Lead Dossier Drawer.
 *
 * The editor is in-house (FloorplanCanvas, plain SVG). It previously embedded
 * the Pascal editor over a postMessage bridge, but Pascal only publishes a
 * read-only viewer embed with "no oEmbed endpoint, JavaScript API, or
 * postMessage interface" — the guest half of that bridge could never exist
 * without forking and hosting their Next.js app. Owning the canvas removes the
 * dependency, keeps Three.js/WebGPU out of the bundle, and makes the whole path
 * testable.
 *
 * Line items are emitted in the exact shape `calculate_underwriting()` accepts,
 * so a saved layout flows into the SAME ARV → rehab → MAO trace as every other
 * underwriting input rather than becoming a parallel source of truth.
 */

const money = (v) =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? '—'
    : `$${Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;

// Friendly labels for the manifest keys backend/floorplan_pipeline/dimensions.py
// always emits. Any key not listed here (the pipeline's open-ended `fields`
// catch-all) falls back to a humanized version of the snake_case name, so a
// future dimension shows up immediately rather than needing a UI change first.
const DIMENSION_LABELS = {
  footprint_area_m2: 'Footprint area',
  footprint_perimeter_m: 'Footprint perimeter',
  levels: 'Levels',
  storey_height_m: 'Storey height',
  wall_height_m: 'Wall height',
  total_height_m: 'Total height',
  exterior_wall_thickness_m: 'Exterior wall thickness',
  interior_wall_thickness_m: 'Interior wall thickness',
  bedrooms: 'Bedrooms',
  bathrooms: 'Bathrooms',
  doors: 'Doors',
  windows: 'Windows',
  total_floor_area_m2: 'Floor area (m²)',
  total_floor_area_sqft: 'Floor area (sq ft)',
};

const humanizeKey = (key) =>
  key.replace(/_m2$/, ' (m²)').replace(/_m$/, ' (m)').replace(/_/g, ' ')
    .replace(/^./, (c) => c.toUpperCase());

const dimensionLabel = (key) => DIMENSION_LABELS[key] || humanizeKey(key);

// Only these two provenances are a guess. 'measured' and 'sourced' both come
// from the world — a footprint ring, an OSM attribute — so neither gets the
// ≈ treatment; only what the pipeline had to invent does.
const isGuess = (provenance) => provenance === 'estimated' || provenance === 'default';

const formatDimensionValue = (dim) => {
  const n = Number(dim.value);
  const rounded = Number.isInteger(n) ? n : Math.round(n * 100) / 100;
  return `${rounded.toLocaleString('en-US')} ${dim.unit}`;
};

const signed = (v) => {
  const n = Number(v) || 0;
  if (Math.abs(n) < 1) return null;
  return `${n > 0 ? '+' : '−'}${money(Math.abs(n))}`;
};

export function RehabEditorDrawer({ leadId, listingId, onClose, onSaved }) {
  const subjectQs = useMemo(
    () => (leadId ? `lead_id=${leadId}` : `listing_id=${listingId}`),
    [leadId, listingId],
  );

  const [loaded, setLoaded] = useState(null);   // FloorplanDocument | null
  const [loadState, setLoadState] = useState('loading'); // loading|ready|error
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState(null);

  // Per-dimension provenance (DimensionManifest | null) and the hash of the
  // machine scaffold this document originated from, if any. Both persist
  // alongside the document (migration 0075) so a reload keeps answering
  // "where did this number come from" — not just the moment auto-fill ran.
  // While viewingRevision is set these describe the REVISION being browsed,
  // not the live head — see viewRevision/returnToCurrent below.
  const [manifest, setManifest] = useState(null);
  const [scaffoldSha256, setScaffoldSha256] = useState(null);
  const [floorplanId, setFloorplanId] = useState(null);

  // History browsing. `headSnapshot` is the live head's own document +
  // provenance, kept out of state (nothing needs to re-render off it) so
  // returning from a revision restores it without a second network round
  // trip. `viewingRevision` set means the canvas is read-only and showing
  // someone else's past, not the editable present.
  const headSnapshot = useRef(null);
  const [viewingRevision, setViewingRevision] = useState(null);
  const [revisionsOpen, setRevisionsOpen] = useState(false);
  const [revisionsList, setRevisionsList] = useState(null);
  const [revisionsLoading, setRevisionsLoading] = useState(false);

  // Fetch the persisted plan so the editor opens on the saved layout rather
  // than an empty grid.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const payload = await crmGet(`/api/crm/floorplan?${subjectQs}`);
        if (cancelled) return;
        const document = payload?.document || EMPTY_FLOORPLAN;
        const loadedManifest = payload?.dimension_manifest ?? null;
        const loadedScaffoldSha256 = payload?.scaffold_sha256 ?? null;
        setLoaded(document);
        setManifest(loadedManifest);
        setScaffoldSha256(loadedScaffoldSha256);
        setFloorplanId(payload?.floorplan_id ?? null);
        headSnapshot.current = { document, manifest: loadedManifest, scaffoldSha256: loadedScaffoldSha256 };
        setLoadState('ready');
      } catch (err) {
        if (cancelled) return;
        setLoadState('error');
        setNotice({ tone: 'error', text: err?.message || 'Could not load the saved layout.' });
      }
    })();
    return () => { cancelled = true; };
  }, [subjectQs]);

  // The editor is in-house now: no iframe, no cross-origin handshake, and no
  // third-party editor that has to be reachable for the pane to work at all.
  const {
    document: planDocument,
    metrics,
    baselineMetrics,
    dirty,
    selectedId,
    select,
    addWall,
    addRoom,
    remove,
    undo,
    load,
    requestDocument,
    markSaved,
  } = useFloorplanEditor({ initialDocument: loaded, readOnly: viewingRevision !== null });

  const [autoFilling, setAutoFilling] = useState(false);

  // Auto-dimensions: resolve every construction dimension (footprint → levels →
  // room scaffold) server-side and load the result for review. It arrives
  // UNSAVED on purpose — the agent inspects the labelled estimates and decides.
  const autoFill = useCallback(async () => {
    setAutoFilling(true);
    setNotice(null);
    try {
      const result = await crmPost(`/api/crm/floorplan/auto-dimensions?${subjectQs}`);
      load(result.document);
      // Captured here, not thrown away: this is what makes every ≈ in the
      // provenance list below able to say WHY, and what save() below sends
      // back so a reload still knows.
      setManifest(result.manifest ?? null);
      setScaffoldSha256(result.scaffold_sha256 ?? null);
      const estimated = result.estimated_fields?.length ?? 0;
      const source = result.footprint?.found
        ? `footprint from ${result.footprint.source}`
        : 'no footprint found — default plate used';
      setNotice({
        tone: 'ok',
        text: `Auto-filled ${source}; ${estimated} value${estimated === 1 ? '' : 's'} estimated — review before saving.`,
      });
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Auto-fill failed.' });
    } finally {
      setAutoFilling(false);
    }
  }, [subjectQs, load]);

  const rehab = useRehabCalculator({ metrics, baselineMetrics });

  const save = useCallback(async () => {
    setSaving(true);
    setNotice(null);
    try {
      const document = await requestDocument();
      const result = await crmPut(`/api/crm/floorplan?${subjectQs}`, {
        document,
        // Snapshot the line items so this estimate stays reproducible even
        // after the cost table changes.
        rehab_items: rehab.underwritingPayload.rehab_items,
        // Carried through opaquely: the manifest describes the scaffold as
        // generated, and scaffold_sha256 is what a later reader compares
        // against this saved document to tell "accepted unchanged" (poison
        // for training) from "the agent corrected it" (the real signal).
        // Both are null for a hand-drawn plan, which is the honest answer.
        dimension_manifest: manifest,
        scaffold_sha256: scaffoldSha256,
      });
      markSaved(document);
      // The just-saved state IS the new "current" — headSnapshot must move
      // forward with it, or a later "Return to current" would restore the
      // document this save just superseded.
      headSnapshot.current = { document, manifest, scaffoldSha256 };
      setFloorplanId((current) => current ?? result.floorplan_id ?? null);
      setRevisionsList(null); // stale after a new revision exists; refetch on next open
      setNotice({
        tone: 'ok',
        text: `Saved revision ${result.revision} — ${Math.round(result.metrics.total_sqft)} sq ft.`,
      });
      onSaved?.(result);
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Save failed.' });
    } finally {
      setSaving(false);
    }
  }, [
    requestDocument, markSaved, subjectQs, rehab.underwritingPayload.rehab_items, onSaved,
    manifest, scaffoldSha256,
  ]);

  // History. Opening lists revision metadata only (cheap); picking one fetches
  // that revision's full document via the same GET, ?revision= pinned — the
  // route floorplan_api.get_floorplan already supports reading history, this
  // just gives it a UI. The canvas goes read-only for the duration: nothing
  // here writes a new revision, it only browses old ones.
  const openRevisions = useCallback(async () => {
    if (!floorplanId) return;
    setRevisionsOpen(true);
    if (revisionsList) return; // already have it; save() clears this on new writes
    setRevisionsLoading(true);
    try {
      const result = await crmGet(`/api/crm/floorplan/${floorplanId}/revisions`);
      setRevisionsList(result?.revisions ?? []);
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Could not load revision history.' });
    } finally {
      setRevisionsLoading(false);
    }
  }, [floorplanId, revisionsList]);

  const viewRevision = useCallback(async (revisionNumber) => {
    setRevisionsOpen(false);
    setNotice(null);
    try {
      const result = await crmGet(`/api/crm/floorplan?${subjectQs}&revision=${revisionNumber}`);
      load(result.document);
      setManifest(result.dimension_manifest ?? null);
      setScaffoldSha256(result.scaffold_sha256 ?? null);
      setViewingRevision(revisionNumber);
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Could not load that revision.' });
    }
  }, [subjectQs, load]);

  const returnToCurrent = useCallback(() => {
    const snapshot = headSnapshot.current;
    if (snapshot) {
      load(snapshot.document);
      setManifest(snapshot.manifest);
      setScaffoldSha256(snapshot.scaffoldSha256);
    }
    setViewingRevision(null);
  }, [load]);

  // Upload-a-scan: extract-image 422s server-side without a scale, but that
  // round trip is exactly the failure the plan calls out — a wrong scale
  // multiplies every rehab line item while looking entirely plausible, so the
  // gate belongs client-side too, disabling submit rather than letting a
  // blank field silently reach the server.
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFile, setUploadFile] = useState(null);
  const [uploadSqft, setUploadSqft] = useState('');
  const [uploading, setUploading] = useState(false);

  const uploadReady = uploadFile != null && Number(uploadSqft) > 0;

  const submitImageExtract = useCallback(async () => {
    if (!uploadReady) return;
    setUploading(true);
    setNotice(null);
    try {
      const form = new FormData();
      form.append('file', uploadFile);
      const result = await crmUpload(
        `/api/crm/floorplan/extract-image?${subjectQs}&known_total_sqft=${encodeURIComponent(uploadSqft)}`,
        form,
      );
      load(result.document);
      // extract-image has no per-dimension manifest (unlike auto-dimensions)
      // — it returns one confidence score for the whole scan, not a
      // measured/sourced/estimated/default breakdown. A stale manifest from
      // an earlier auto-fill must not linger and be misread as describing
      // this new, unrelated document.
      setManifest(null);
      setScaffoldSha256(result.scaffold_sha256 ?? null);
      setUploadOpen(false);
      setUploadFile(null);
      setUploadSqft('');
      setNotice({
        tone: 'ok',
        text: `Extracted from image — ${Math.round(result.metrics.total_sqft)} sq ft, `
          + `confidence ${Math.round((result.confidence ?? 0) * 100)}%. Review before saving.`,
      });
    } catch (err) {
      const text = err?.status === 503
        // The pipeline imports opencv/numpy lazily so the rest of the API
        // keeps working without them; the honest message here is that this
        // deployment cannot do this specific thing, not a generic failure.
        ? 'Image extraction is not available on this deployment.'
        : err?.message || 'Image extraction failed.';
      setNotice({ tone: 'error', text });
    } finally {
      setUploading(false);
    }
  }, [uploadReady, uploadFile, uploadSqft, subjectQs, load]);

  // Footprint picker — the manual alternative to Auto-fill. Auto-fill already
  // resolves footprint → complete-dimensions internally with no candidate
  // choice exposed; this is for when the agent wants to see and pick between
  // sources themselves (an OSM match a block off vs. the licensed one, say).
  const [footprintOpen, setFootprintOpen] = useState(false);
  const [footprintAddress, setFootprintAddress] = useState('');
  const [footprintCandidates, setFootprintCandidates] = useState(null);
  const [footprintSearching, setFootprintSearching] = useState(false);
  const [footprintApplyingIndex, setFootprintApplyingIndex] = useState(null);

  const searchFootprints = useCallback(async () => {
    if (!footprintAddress.trim()) return;
    setFootprintSearching(true);
    setFootprintCandidates(null);
    setNotice(null);
    try {
      const result = await crmPost(`/api/crm/floorplan/footprint-candidates?${subjectQs}`, {
        address: footprintAddress.trim(),
      });
      setFootprintCandidates(result.candidates ?? []);
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Footprint search failed.' });
    } finally {
      setFootprintSearching(false);
    }
  }, [footprintAddress, subjectQs]);

  const applyFootprint = useCallback(async (candidate, index) => {
    setFootprintApplyingIndex(index);
    setNotice(null);
    try {
      const result = await crmPost(`/api/crm/floorplan/extract-parcel?${subjectQs}`, {
        geometry: candidate.geometry,
      });
      load(result.document);
      // extract-parcel returns an exterior shell only, no per-dimension
      // manifest — same reasoning as extract-image: don't let a stale
      // auto-fill manifest linger and misdescribe this new document.
      setManifest(null);
      setScaffoldSha256(result.scaffold_sha256 ?? null);
      setFootprintOpen(false);
      setFootprintCandidates(null);
      // ODbL requires the credit wherever the geometry is shown — the notice
      // band is the "on screen" this satisfies, not a one-time toast that
      // vanishes before anyone reads it.
      setNotice({
        tone: 'ok',
        text: `Exterior shell from ${candidate.source} — ${Math.round(result.metrics.total_sqft)} sq ft. `
          + `${candidate.attribution} (${candidate.licence}). No interior walls yet — review before saving.`,
      });
    } catch (err) {
      setNotice({ tone: 'error', text: err?.message || 'Could not extract this footprint.' });
    } finally {
      setFootprintApplyingIndex(null);
    }
  }, [subjectQs, load]);

  const deltaLabel = signed(rehab.previewDelta);

  return (
    <div className={styles.overlay} role="dialog" aria-modal="true" aria-label="Floor plan and rehab estimate">
      <header className={styles.header}>
        <div>
          <h2 className={styles.title}>Floor plan &amp; rehab</h2>
          <p className={styles.sub}>
            Edit the layout — line items and the rehab total update as you draw.
          </p>
        </div>
        <div className={styles.headerActions}>
          {floorplanId ? (
            <button
              type="button"
              className={styles.close}
              onClick={openRevisions}
              disabled={viewingRevision !== null}
              title="Browse past revisions (read-only)"
            >
              <Clock aria-hidden="true" />
              History
            </button>
          ) : null}
          <button
            type="button"
            className={styles.close}
            onClick={autoFill}
            disabled={autoFilling || saving || loadState !== 'ready' || viewingRevision !== null}
            title="Resolve footprint, levels and a room scaffold from property data"
          >
            {autoFilling ? <Loader2 className={styles.spin} aria-hidden="true" /> : <Wand2 aria-hidden="true" />}
            {autoFilling ? 'Resolving…' : 'Auto-fill'}
          </button>
          <button
            type="button"
            className={styles.close}
            onClick={() => setUploadOpen((open) => !open)}
            disabled={saving || loadState !== 'ready' || viewingRevision !== null}
            title="Extract a layout from an uploaded floor-plan scan"
          >
            <ImageUp aria-hidden="true" />
            Upload scan
          </button>
          <button
            type="button"
            className={styles.close}
            onClick={() => setFootprintOpen((open) => !open)}
            disabled={saving || loadState !== 'ready' || viewingRevision !== null}
            title="Choose a building footprint from licensed and open sources"
          >
            <MapPinned aria-hidden="true" />
            Find footprint
          </button>
          {dirty && !saving ? (
            <span className={styles.sub} role="status">Unsaved changes</span>
          ) : null}
          <button
            type="button"
            className={styles.save}
            onClick={save}
            disabled={saving || loadState !== 'ready' || !dirty || viewingRevision !== null}
          >
            {saving ? <Loader2 className={styles.spin} aria-hidden="true" /> : <Save aria-hidden="true" />}
            {saving ? 'Saving…' : 'Save layout'}
          </button>
          <button type="button" className={styles.close} onClick={onClose} aria-label="Close editor">
            <X aria-hidden="true" />
          </button>
        </div>
      </header>

      {viewingRevision !== null ? (
        <p className={styles.historyBanner} role="status">
          Viewing revision {viewingRevision} — read-only.
          <button type="button" onClick={returnToCurrent}>Return to current</button>
        </p>
      ) : null}

      {revisionsOpen ? (
        <div className={styles.revisionsPanel} role="dialog" aria-label="Revision history">
          <div className={styles.revisionsPanelHead}>
            <span>Revision history</span>
            <button type="button" className={styles.close} onClick={() => setRevisionsOpen(false)} aria-label="Close revision history">
              <X aria-hidden="true" />
            </button>
          </div>
          {revisionsLoading ? (
            <p className={styles.muted}><Loader2 className={styles.spin} aria-hidden="true" /> Loading…</p>
          ) : !revisionsList || revisionsList.length === 0 ? (
            <p className={styles.muted}>No saved revisions yet.</p>
          ) : (
            <ul className={styles.revisionsList}>
              {revisionsList.map((rev) => (
                <li key={rev.revision}>
                  <button type="button" onClick={() => viewRevision(rev.revision)}>
                    <span className={styles.revisionNumber}>Rev {rev.revision}</span>
                    <span>{Math.round(rev.total_sqft).toLocaleString('en-US')} sq ft</span>
                    <span className={styles.muted}>{new Date(rev.created_at).toLocaleString()}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}

      {uploadOpen ? (
        <div className={styles.revisionsPanel} role="dialog" aria-label="Upload a floor-plan scan">
          <div className={styles.revisionsPanelHead}>
            <span>Upload scan</span>
            <button type="button" className={styles.close} onClick={() => setUploadOpen(false)} aria-label="Close upload panel">
              <X aria-hidden="true" />
            </button>
          </div>
          <div className={styles.uploadBody}>
            <p className={styles.muted}>
              A wrong scale multiplies every line item while looking plausible — the
              total square footage this house is known to have keeps that from happening.
            </p>
            <label className={styles.uploadField}>
              <span>Floor-plan image</span>
              <input
                type="file"
                accept="image/*"
                onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
              />
            </label>
            <label className={styles.uploadField}>
              <span>Known total square footage</span>
              <input
                type="number"
                min="1"
                step="1"
                placeholder="e.g. 1800"
                value={uploadSqft}
                onChange={(e) => setUploadSqft(e.target.value)}
              />
            </label>
            <button
              type="button"
              className={styles.save}
              onClick={submitImageExtract}
              disabled={!uploadReady || uploading}
            >
              {uploading ? <Loader2 className={styles.spin} aria-hidden="true" /> : <ImageUp aria-hidden="true" />}
              {uploading ? 'Extracting…' : 'Extract layout'}
            </button>
          </div>
        </div>
      ) : null}

      {footprintOpen ? (
        <div className={styles.revisionsPanel} role="dialog" aria-label="Find a building footprint">
          <div className={styles.revisionsPanelHead}>
            <span>Find footprint</span>
            <button type="button" className={styles.close} onClick={() => setFootprintOpen(false)} aria-label="Close footprint search">
              <X aria-hidden="true" />
            </button>
          </div>
          <div className={styles.uploadBody}>
            <label className={styles.uploadField}>
              <span>Address</span>
              <input
                type="text"
                placeholder="123 Main St, Dover, DE"
                value={footprintAddress}
                onChange={(e) => setFootprintAddress(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') searchFootprints(); }}
              />
            </label>
            <button
              type="button"
              className={styles.save}
              onClick={searchFootprints}
              disabled={!footprintAddress.trim() || footprintSearching}
            >
              {footprintSearching ? <Loader2 className={styles.spin} aria-hidden="true" /> : <MapPinned aria-hidden="true" />}
              {footprintSearching ? 'Searching…' : 'Search'}
            </button>

            {footprintCandidates && footprintCandidates.length === 0 ? (
              <p className={styles.muted}>
                No building outline found for this address — rural OSM coverage is
                patchy and not every address matches a licensed record.
              </p>
            ) : null}

            {footprintCandidates && footprintCandidates.length > 0 ? (
              <ul className={styles.revisionsList}>
                {footprintCandidates.map((candidate, index) => (
                  // Index as key: candidates carry no stable id from the source,
                  // and the list is replaced wholesale on every new search.
                  <li key={index}>
                    <button
                      type="button"
                      onClick={() => applyFootprint(candidate, index)}
                      disabled={footprintApplyingIndex !== null}
                    >
                      <span className={styles.revisionNumber}>
                        {candidate.name || `${candidate.building_type || 'Building'} footprint`}
                      </span>
                      <span>
                        {Math.round(candidate.area_sqm * 10.7639).toLocaleString('en-US')} sq ft
                        {candidate.levels ? ` · ${candidate.levels} level${candidate.levels === 1 ? '' : 's'}` : ''}
                      </span>
                      {/* ODbL requires the credit wherever the geometry is shown —
                          this is that credit, on the candidate itself, before
                          any choice is made, not only after one is applied. */}
                      <span className={styles.muted}>{candidate.source} · {candidate.licence}</span>
                      <span className={styles.muted}>{candidate.attribution}</span>
                      {footprintApplyingIndex === index ? (
                        <span className={styles.muted}><Loader2 className={styles.spin} aria-hidden="true" /> Extracting…</span>
                      ) : null}
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </div>
      ) : null}

      {notice ? (
        <p className={notice.tone === 'error' ? styles.error : styles.ok} role="status">{notice.text}</p>
      ) : null}

      <div className={styles.body}>
        {/* ── editor ── */}
        <div className={styles.canvasPane}>
          {loadState === 'loading' ? (
            <div className={styles.frameOverlay}>
              <p><Loader2 className={styles.spin} aria-hidden="true" /> Loading the saved layout…</p>
            </div>
          ) : (
            <FloorplanCanvas
              document={planDocument}
              selectedId={selectedId}
              onSelect={select}
              onAddWall={addWall}
              onAddRoom={addRoom}
              onRemove={remove}
              onUndo={undo}
            />
          )}
        </div>

        {/* ── live estimate ── */}
        <aside className={styles.costPane} aria-label="Rehab estimate">
          <div className={styles.totals}>
            <div className={styles.totalBlock}>
              <span className={styles.totalLabel}>Rehab estimate</span>
              <span className={styles.totalValue}>{money(rehab.previewTotal)}</span>
              {deltaLabel ? (
                <span className={rehab.previewDelta > 0 ? styles.deltaUp : styles.deltaDown}>
                  {deltaLabel} since last save
                </span>
              ) : null}
            </div>
            <dl className={styles.spatialFacts}>
              <div><dt>Floor area</dt><dd>{rehab.totalSqft.toLocaleString('en-US')} sq ft</dd></div>
              <div><dt>Wall run</dt><dd>{Math.round(rehab.imperial.wall_linear_ft).toLocaleString('en-US')} ft</dd></div>
              <div><dt>Rooms</dt><dd>{rehab.imperial.counts.rooms}</dd></div>
              <div><dt>Openings</dt><dd>{rehab.imperial.counts.doors + rehab.imperial.counts.windows}</dd></div>
            </dl>
          </div>

          <p className={styles.authority}>
            Preview only — the server recomputes this in exact decimal on save and
            feeds it to ARV → rehab → MAO.
          </p>

          {manifest ? (
            <div className={styles.provenance}>
              <div className={styles.provenanceHead}>
                <span className={styles.provenanceTitle}>Dimension provenance</span>
                {dirty ? (
                  <span
                    className={styles.provenanceStale}
                    title="The layout has unsaved edits — some of these values may no longer match the drawn geometry."
                  >
                    may be stale
                  </span>
                ) : null}
              </div>
              <dl className={styles.provenanceList}>
                {Object.entries(manifest).map(([key, dim]) => (
                  <div key={key} data-guess={isGuess(dim.provenance)}>
                    <dt>{dimensionLabel(key)}</dt>
                    <dd title={isGuess(dim.provenance) ? dim.basis : undefined}>
                      {isGuess(dim.provenance) ? '≈' : ''}
                      {formatDimensionValue(dim)}
                      {dim.provenance === 'sourced' ? (
                        <span className={styles.provenanceChip}>{dim.basis}</span>
                      ) : null}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          ) : null}

          <ul className={styles.lineItems}>
            {rehab.lines.map((line) => (
              <li key={line.key} data-off={!line.enabled}>
                <label className={styles.lineHead}>
                  <input
                    type="checkbox"
                    checked={line.enabled}
                    onChange={(e) => rehab.toggleLine(line.key, e.target.checked)}
                  />
                  <span className={styles.lineLabel}>{line.label}</span>
                  <span className={styles.lineSubtotal}>{money(line.subtotal)}</span>
                </label>
                <div className={styles.lineMeta}>
                  <span>{line.quantity.toLocaleString('en-US')} {line.unit}</span>
                  <span aria-hidden="true">×</span>
                  <label className={styles.unitCost}>
                    <span className={styles.srOnly}>{line.label} unit cost</span>
                    $
                    <input
                      type="number"
                      min="0"
                      step="0.05"
                      value={line.unitCost}
                      onChange={(e) => rehab.setUnitCost(line.key, Number(e.target.value))}
                    />
                  </label>
                  {line.delta ? (
                    <span className={line.delta > 0 ? styles.deltaUp : styles.deltaDown}>
                      {signed(line.delta)}
                    </span>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>

          {rehab.hasOverrides ? (
            <button type="button" className={styles.reset} onClick={rehab.resetOverrides}>
              Reset unit costs to defaults
            </button>
          ) : null}

          {loadState === 'error' ? (
            <p className={styles.muted}>Starting from an empty layout.</p>
          ) : null}
        </aside>
      </div>
    </div>
  );
}

export default RehabEditorDrawer;
