import { useCallback, useEffect, useMemo, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './DossierPanel.module.css';

/**
 * Run a pre-distress analysis against cited public-record evidence.
 *
 * This is the half of `intelligence_api` that could not exist before. Every POST
 * on that router requires `sources` — at least one `source_record_id` that
 * `_verified_citations()` resolves against the immutable `source_records`
 * table — and nothing listed that table, so a person had no way to discover the
 * UUIDs an analysis must cite. Thirteen routes sat unreachable behind one
 * absent SELECT. `GET /api/intelligence/sources` is that SELECT.
 *
 * Two rules this screen keeps, because breaking either would make the result a
 * claim rather than a finding:
 *
 * 1. **Evidence is chosen, not implied.** The analysis is only as good as the
 *    records behind it, so the records are picked explicitly and the citation
 *    object is passed through byte-for-byte from the listing. The server
 *    re-derives every citation from the database anyway and treats what the
 *    client sends as a display hint, so inventing one here would be discarded —
 *    but it would also mislead the person filling the form.
 *
 * 2. **The signal vocabulary comes from the server.** `score_pre_distress()`
 *    rejects any name it does not recognise and the weights decide what the
 *    score means, so a hardcoded copy here would drift the moment a signal is
 *    added. `GET /api/intelligence/policy` serves both.
 */

function shortDate(value) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return '';
  return parsed.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function label(signal) {
  return signal.replace(/_/g, ' ');
}

export default function IntelligenceAuthoring({ propertyKey, onAuthored }) {
  const [sources, setSources] = useState(null);
  const [signals, setSignals] = useState([]);
  const [selected, setSelected] = useState(() => new Set());
  const [values, setValues] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(() => {
    if (!propertyKey) return undefined;
    setError('');
    return Promise.all([
      crmGet(`/api/intelligence/sources?property_key=${encodeURIComponent(propertyKey)}&limit=100`),
      crmGet('/api/intelligence/policy'),
    ]).then(
      ([listing, policy]) => {
        setSources(Array.isArray(listing?.citable) ? listing.citable : []);
        setSignals(Array.isArray(policy?.distress_signals) ? policy.distress_signals : []);
      },
      (reason) => {
        setSources([]);
        setError(reason?.message || 'Citable source records could not be read.');
      },
    );
  }, [propertyKey]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  const citable = useMemo(
    () => (sources || []).filter((row) => row?.cite?.source_record_id),
    [sources],
  );

  const toggle = (row) => {
    if (!row.property_level_allowed) return;
    const id = row.cite.source_record_id;
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const setSignal = (name, raw) => {
    setValues((prev) => ({ ...prev, [name]: raw }));
  };

  // 0..1 each, and at least one — an empty signal set scores zero at zero
  // coverage, which is a number that looks like a finding and is not one.
  const scored = Object.entries(values)
    .map(([name, raw]) => [name, Number(raw)])
    .filter(([, value]) => Number.isFinite(value) && value >= 0 && value <= 1);
  const ready = selected.size > 0 && scored.length > 0 && !busy;

  const run = async () => {
    if (!ready) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await crmPost('/api/intelligence/pre-distress', {
        property_key: propertyKey,
        // Passed through exactly as the listing returned it. SourceCitation
        // sets extra="forbid", so an added field is a 422.
        sources: citable
          .filter((row) => selected.has(row.cite.source_record_id))
          .map((row) => row.cite),
        signals: Object.fromEntries(scored),
      });
      setValues({});
      setSelected(new Set());
      setNotice('Analysis recorded against the cited records.');
      await onAuthored?.();
    } catch (reason) {
      // apiClient lifts FastAPI's nested {"detail":{"code":...}} onto
      // ApiError.code; there is no `.body` on the error.
      const code = reason?.code;
      if (code === 'SOURCE_LICENSE_FORBIDS_PROPERTY_USE') {
        setError('One of those records carries a licence that forbids property-level use.');
      } else if (code === 'SOURCE_RECORD_NOT_VISIBLE') {
        setError('A cited record is no longer visible to this tenant. Reload the evidence list.');
      } else {
        setError(reason?.message || 'The analysis was refused.');
      }
    } finally {
      setBusy(false);
    }
  };

  if (!propertyKey) return null;

  return (
    <section className={styles.section} aria-label="Run analysis">
      <h3 className={styles.kicker}>Run analysis · pre-distress</h3>

      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      {notice ? <p className={styles.sourceNote} role="status">{notice}</p> : null}
      {sources === null && !error ? <p className={styles.loading}>READING EVIDENCE…</p> : null}

      {sources !== null && citable.length === 0 ? (
        <p className={styles.emptyNote}>
          No public-record observations have been retained for this property, so there is
          nothing an analysis could cite. Records arrive from the harvesters — if a
          jurisdiction shows no facts at all, its data credential has usually lapsed rather
          than the property being unusual. Admin → data source health says which.
        </p>
      ) : null}

      {citable.length > 0 ? (
        <>
          <ul className={styles.evidenceList} role="list">
            {citable.map((row) => {
              const id = row.cite.source_record_id;
              const blocked = !row.property_level_allowed;
              return (
                <li
                  key={id}
                  className={styles.evidenceItem}
                  data-selected={selected.has(id) ? 'true' : 'false'}
                  data-blocked={blocked ? 'true' : 'false'}
                >
                  <label>
                    <input
                      type="checkbox"
                      checked={selected.has(id)}
                      disabled={blocked}
                      onChange={() => toggle(row)}
                    />
                    <span className="sr-only">Cite {row.cite.source}</span>
                  </label>
                  <span>
                    <strong>{row.cite.source}</strong>
                    <span className={styles.evidenceMeta}>
                      {row.cite.record_id || 'no record id'}
                      {' · observed '}{shortDate(row.cite.observed_at) || 'undated'}
                      {' · '}{row.cite.license}
                      {row.payload_purged ? ' · payload purged (provenance kept)' : ''}
                      {/* Not a block on analysis — this data may describe the
                          property, just not be used to contact its owner. */}
                      {row.outreach_use_allowed === false ? ' · not lawful outreach material' : ''}
                      {blocked ? ' · licence forbids property-level use' : ''}
                    </span>
                  </span>
                </li>
              );
            })}
          </ul>

          <div className={styles.signalGrid}>
            {signals.map(({ signal, weight }) => (
              <label key={signal} className={styles.field}>
                <span>{label(signal)} · w{weight}</span>
                <input
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  inputMode="decimal"
                  value={values[signal] ?? ''}
                  onChange={(event) => setSignal(signal, event.target.value)}
                  placeholder="0–1"
                />
              </label>
            ))}
          </div>

          <button type="button" className={styles.synthesize} onClick={run} disabled={!ready}>
            {busy ? 'Scoring…' : `Score against ${selected.size || 'no'} record${selected.size === 1 ? '' : 's'}`}
          </button>

          <p className={styles.authorNote}>
            Leave a signal blank when you have no observation for it. A blank is not a zero —
            the engine weights only what you filled in and reports the coverage, so an
            invented zero would quietly raise confidence in a score nothing supports.
          </p>
        </>
      ) : null}
    </section>
  );
}
