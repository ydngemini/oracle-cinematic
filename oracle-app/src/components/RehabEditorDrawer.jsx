import { useCallback, useEffect, useMemo, useState } from 'react';
import { Loader2, Save, Wand2, X } from 'lucide-react';
import { crmGet, crmPost, crmPut } from '../state/useCrmApi';
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

  // Fetch the persisted plan so the editor opens on the saved layout rather
  // than an empty grid.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const payload = await crmGet(`/api/crm/floorplan?${subjectQs}`);
        if (cancelled) return;
        setLoaded(payload?.document || EMPTY_FLOORPLAN);
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
  } = useFloorplanEditor({ initialDocument: loaded, readOnly: false });

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
      });
      markSaved(document);
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
  }, [requestDocument, markSaved, subjectQs, rehab.underwritingPayload.rehab_items, onSaved]);

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
          <button
            type="button"
            className={styles.close}
            onClick={autoFill}
            disabled={autoFilling || saving || loadState !== 'ready'}
            title="Resolve footprint, levels and a room scaffold from property data"
          >
            {autoFilling ? <Loader2 className={styles.spin} aria-hidden="true" /> : <Wand2 aria-hidden="true" />}
            {autoFilling ? 'Resolving…' : 'Auto-fill'}
          </button>
          {dirty && !saving ? (
            <span className={styles.sub} role="status">Unsaved changes</span>
          ) : null}
          <button
            type="button"
            className={styles.save}
            onClick={save}
            disabled={saving || loadState !== 'ready' || !dirty}
          >
            {saving ? <Loader2 className={styles.spin} aria-hidden="true" /> : <Save aria-hidden="true" />}
            {saving ? 'Saving…' : 'Save layout'}
          </button>
          <button type="button" className={styles.close} onClick={onClose} aria-label="Close editor">
            <X aria-hidden="true" />
          </button>
        </div>
      </header>

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
