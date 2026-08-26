import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import styles from './DossierPanel.module.css';

/**
 * Stored intelligence for one property.
 *
 * `intelligence_api` is the largest capability in this codebase that no screen
 * ever reached: 13 routes covering underwriting, preliminary title, micro-market
 * forecast, pre-distress scoring, highest-and-best-use, entity graph, and the
 * spatial inferences beside them. Every analysis it has ever produced was
 * written to `intelligence_scores` and read back by nobody.
 *
 * This surfaces the READ half — what has been analysed, how confident it was,
 * what evidence backed it, and whether a professional has reviewed it.
 *
 * The authoring half is deliberately absent, and the reason is a real blocker
 * rather than a decision: every POST extends AnalysisBase, which requires
 * `sources` — at least one `source_record_id` that `_verified_citations()`
 * resolves against the `source_records` table. **No endpoint lists that table**,
 * so a person using this product has no way to discover the UUIDs an analysis
 * must cite. A form here could be filled in and never submitted. Wiring
 * authoring needs a source-record listing endpoint first.
 */

const TYPE_LABEL = {
  pre_distress: 'Pre-distress',
  highest_best_use: 'Highest & best use',
  underwriting: 'Underwriting',
  title: 'Preliminary title',
  forecast: 'Market forecast',
  detectors: 'Detectors',
  entity_graph: 'Entity graph',
  characteristic_imputation: 'Imputed characteristics',
  photo_rehab: 'Photo rehab estimate',
  topography: 'Topography',
  tour_variants: 'Tour variants',
};

function when(value) {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return null;
  return parsed.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function percent(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return null;
  return `${Math.round(parsed * 100)}%`;
}

export default function PropertyIntelligencePanel({ propertyKey }) {
  const [analyses, setAnalyses] = useState(null);
  const [error, setError] = useState('');

  const load = useCallback(() => {
    if (!propertyKey) return undefined;
    setError('');
    return crmGet(`/api/intelligence/${encodeURIComponent(propertyKey)}?limit=25`).then(
      (payload) => setAnalyses(Array.isArray(payload?.analyses) ? payload.analyses : []),
      (reason) => {
        // 404 means nothing has been analysed, which is a normal state for a
        // property nobody has run an analysis against.
        if (reason?.status === 404) {
          setAnalyses([]);
          return;
        }
        setError(reason?.message || 'Stored intelligence could not be read.');
      },
    );
  }, [propertyKey]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  if (!propertyKey) return null;

  return (
    <section className={styles.section} aria-label="Property intelligence">
      <h3 className={styles.kicker}>Intelligence</h3>

      {error ? <p className={styles.error}>{error}</p> : null}
      {analyses === null && !error ? <p className={styles.loading}>READING ANALYSES…</p> : null}

      {analyses !== null && analyses.length === 0 ? (
        <p className={styles.provenance}>
          No analysis has been run against this property. Underwriting, title, forecast and
          distress scoring all require cited source records, and nothing in the product lists
          them yet — so these are produced by the API directly, not from this screen.
        </p>
      ) : null}

      {analyses !== null && analyses.length > 0 ? (
        <ul className={styles.provenance}>
          {analyses.map((row) => (
            <li key={row.id}>
              <strong>{TYPE_LABEL[row.analysis_type] || row.analysis_type}</strong>
              {' · '}
              {when(row.observation_date) || when(row.created_at) || 'undated'}
              {percent(row.confidence) ? ` · ${percent(row.confidence)} confidence` : ''}
              {row.evidence_status ? ` · evidence ${row.evidence_status}` : ''}
              {/* Professional review is the difference between a model's opinion
                  and something an agent may repeat to a client. Say which. */}
              {row.professional_review_status
                ? ` · review ${row.professional_review_status}`
                : ' · not reviewed'}
              {row.model_version ? ` · ${row.model_version}` : ''}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
