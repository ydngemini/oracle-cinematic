import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { useStateCtx } from '../state/StateContext';
import styles from './ComplianceDashboard.module.css';

const GLYPHS = {
  check: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12.5 10 17.5 19 7" />
    </svg>
  ),
  alert: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3 2 20h20Z" />
      <path d="M12 9.5v4M12 16.5h.01" />
    </svg>
  ),
  expand: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m6 9 6 6 6-6" />
    </svg>
  ),
};

const SEVERITY_LABELS = { required: 'Required', recommended: 'Recommended', optional: 'Optional' };

export function ComplianceDashboard({ listingId }) {
  const { primaryState } = useStateCtx();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [completed, setCompleted] = useState(() => {
    try {
      const stored = sessionStorage.getItem(`oracle_compliance_${listingId}`);
      return stored ? JSON.parse(stored) : {};
    } catch { return {}; }
  });
  const [expanded, setExpanded] = useState(null);

  const fetch_ = useCallback(() => {
    if (!listingId) return;
    const stateParam = primaryState ? `&state=${primaryState}` : '';
    crmGet(`/api/compliance/check?listing_id=${listingId}${stateParam}`).then(
      (res) => { setData(res); setLoading(false); setError(null); },
      (err) => { setError(err); setLoading(false); }
    );
  }, [listingId, primaryState]);

  // Show the spinner again when the target changes — render-phase reset, so the
  // fetch effect below carries no synchronous setState (set-state-in-effect).
  const ccKey = `${listingId} ${primaryState}`;
  const [prevCcKey, setPrevCcKey] = useState(ccKey);
  if (ccKey !== prevCcKey) { setPrevCcKey(ccKey); setLoading(Boolean(listingId)); setError(null); }

  useEffect(() => { fetch_(); }, [fetch_]);

  const toggleItem = (id) => {
    setCompleted((prev) => {
      const next = { ...prev, [id]: !prev[id] };
      sessionStorage.setItem(`oracle_compliance_${listingId}`, JSON.stringify(next));
      return next;
    });
  };

  if (!listingId) return null;

  const disclosures = data?.disclosures || [];
  const totalCount = disclosures.length;
  const doneCount = disclosures.filter((d) => completed[d.id]).length;
  const pct = totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0;

  return (
    <section className={styles.wrap} aria-label="Compliance disclosures">
      <header className={styles.header}>
        <span className={styles.title}>State Compliance</span>
        {primaryState && <span className={styles.stateBadge}>{primaryState}</span>}
      </header>

      {loading ? (
        <div className={styles.skeleton} aria-hidden="true">
          <div className={styles.skelBar} />
          <div className={styles.skelRow} />
          <div className={styles.skelRow} />
        </div>
      ) : error ? (
        <div className={styles.errorBox} role="alert">
          <span className={styles.errorText}>
            {error.status === 404 ? 'Compliance service not deployed.' : error.message}
          </span>
          <button type="button" className={styles.retryBtn} onClick={fetch_}>Retry</button>
        </div>
      ) : (
        <>
          <div className={styles.progressWrap}>
            <div className={styles.progressBar}>
              <div className={styles.progressFill} style={{ width: `${pct}%` }} />
            </div>
            <span className={styles.progressLabel}>{doneCount}/{totalCount} complete</span>
          </div>

          <ul className={styles.list}>
            {disclosures.map((item) => (
              <li key={item.id} className={styles.item}>
                <button
                  type="button"
                  className={styles.itemRow}
                  onClick={() => toggleItem(item.id)}
                >
                  <span className={`${styles.checkbox} ${completed[item.id] ? styles.checkboxDone : ''}`}>
                    {completed[item.id] && <span className={styles.checkGlyph}>{GLYPHS.check}</span>}
                  </span>
                  <span className={styles.itemName}>{item.name}</span>
                  <span className={`${styles.severity} ${styles[`sev_${item.severity}`]}`}>
                    {SEVERITY_LABELS[item.severity] || item.severity}
                  </span>
                </button>

                <button
                  type="button"
                  className={`${styles.expandBtn} ${expanded === item.id ? styles.expandBtnOpen : ''}`}
                  onClick={() => setExpanded((p) => (p === item.id ? null : item.id))}
                  aria-label="Details"
                >
                  {GLYPHS.expand}
                </button>

                {expanded === item.id && (
                  <div className={styles.detail}>
                    {item.description && <p className={styles.detailText}>{item.description}</p>}
                    {item.deadline && <p className={styles.deadline}>Due: {item.deadline}</p>}
                  </div>
                )}
              </li>
            ))}
          </ul>

          {totalCount === 0 && (
            <p className={styles.emptyText}>No disclosures required for this state/property type.</p>
          )}
        </>
      )}
    </section>
  );
}
