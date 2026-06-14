import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import styles from './MlsFeedStatus.module.css';

const GLYPHS = {
  signal: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 20h.01M7 20v-4M12 20v-8M17 20v-12M22 20v-16" />
    </svg>
  ),
  refresh: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20.5 12a8.5 8.5 0 1 1-2.49-6.01" />
      <path d="M20.5 3.5V8H16" />
    </svg>
  ),
};

const fmtInt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

function freshnessClass(lastSync) {
  if (!lastSync) return styles.statusRed;
  const hours = (Date.now() - new Date(lastSync).getTime()) / 3600000;
  if (hours < 1) return styles.statusGreen;
  if (hours < 6) return styles.statusAmber;
  return styles.statusRed;
}

function freshnessLabel(lastSync) {
  if (!lastSync) return 'Never';
  const hours = (Date.now() - new Date(lastSync).getTime()) / 3600000;
  if (hours < 1) return `${Math.round(hours * 60)}m ago`;
  if (hours < 24) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function Sparkline({ points }) {
  if (!points || points.length < 2) return null;
  const max = Math.max(...points, 1);
  const w = 80;
  const h = 20;
  const coords = points.map((p, i) => {
    const x = (i / (points.length - 1)) * w;
    const y = h - (p / max) * h;
    return `${x},${y}`;
  });
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className={styles.sparkline} aria-hidden="true">
      <polyline points={coords.join(' ')} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

export function MlsFeedStatus() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expandedId, setExpandedId] = useState(null);

  const fetch_ = useCallback(() => {
    setLoading(true);
    crmGet('/api/mls/regions').then(
      (res) => { setData(res); setLoading(false); setError(null); },
      (err) => { setError(err); setLoading(false); }
    );
  }, []);

  useEffect(() => { fetch_(); }, [fetch_]);

  const regions = data?.regions || [];

  return (
    <section className={styles.wrap} aria-label="MLS feed status">
      <header className={styles.header}>
        <span className={styles.headerGlyph}>{GLYPHS.signal}</span>
        <span className={styles.title}>MLS Feed Health</span>
        <button type="button" className={styles.refreshBtn} onClick={fetch_} aria-label="Refresh">
          {GLYPHS.refresh}
        </button>
      </header>

      {loading ? (
        <div className={styles.skeleton} aria-hidden="true">
          <div className={styles.skelCard} />
          <div className={styles.skelCard} />
          <div className={styles.skelCard} />
        </div>
      ) : error ? (
        <div className={styles.errorBox} role="alert">
          <span className={styles.errorText}>
            {error.status === 404 ? 'MLS service not deployed.' : error.message}
          </span>
          <button type="button" className={styles.retryBtn} onClick={fetch_}>Retry</button>
        </div>
      ) : (
        <>
          <div className={styles.grid}>
            {regions.map((r) => (
              <div key={r.id || r.mls_id} className={styles.card}>
                <div className={styles.cardTop}>
                  <span className={`${styles.statusDot} ${freshnessClass(r.last_sync)}`} />
                  <span className={styles.mlsName}>{r.mls_name || r.mls_id}</span>
                  <span className={styles.stateChip}>{r.primary_state}</span>
                </div>
                <div className={styles.cardMid}>
                  <span className={styles.meta}>
                    {r.record_count != null ? `${fmtInt.format(r.record_count)} records` : '—'}
                  </span>
                  <span className={styles.meta}>{freshnessLabel(r.last_sync)}</span>
                </div>
                {r.sparkline_data && (
                  <Sparkline points={r.sparkline_data} />
                )}
                {r.errors && r.errors.length > 0 && (
                  <>
                    <button
                      type="button"
                      className={styles.errToggle}
                      onClick={() => setExpandedId((p) => (p === r.id ? null : r.id))}
                    >
                      {r.errors.length} error{r.errors.length > 1 ? 's' : ''}
                    </button>
                    {expandedId === r.id && (
                      <ul className={styles.errList}>
                        {r.errors.slice(0, 5).map((e, i) => (
                          <li key={i} className={styles.errItem}>{e}</li>
                        ))}
                      </ul>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>

          {regions.length === 0 && (
            <p className={styles.emptyText}>No MLS feeds configured.</p>
          )}
        </>
      )}
    </section>
  );
}
