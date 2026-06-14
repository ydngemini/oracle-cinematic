import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { useStateCtx } from '../state/StateContext';
import styles from './MarketDataPanels.module.css';

const GLYPHS = {
  trendUp: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M7 17 17 7" />
      <path d="M10 7h7v7" />
    </svg>
  ),
  trendDown: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M7 7 17 17" />
      <path d="M10 17h7v-7" />
    </svg>
  ),
  flood: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 17c2-2 4-2 6 0s4 2 6 0 4-2 6 0" />
      <path d="M3 12c2-2 4-2 6 0s4 2 6 0 4-2 6 0" />
      <path d="M12 3v6" />
    </svg>
  ),
};

const fmtPrice = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const fmtPct = new Intl.NumberFormat('en-US', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 });
const fmtInt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

function TrendArrow({ value }) {
  if (value == null || value === 0) return null;
  const positive = value > 0;
  return (
    <span className={`${styles.trend} ${positive ? styles.trendPositive : styles.trendNegative}`}>
      <span className={styles.trendGlyph}>{positive ? GLYPHS.trendUp : GLYPHS.trendDown}</span>
      {fmtPct.format(Math.abs(value) / 100)}
    </span>
  );
}

export function MarketDataPanels() {
  const { primaryState } = useStateCtx();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetch_ = useCallback(() => {
    if (!primaryState) { setLoading(false); return; }
    setLoading(true);
    crmGet(`/api/market/${primaryState}/overview`).then(
      (res) => { setData(res); setLoading(false); setError(null); },
      (err) => { setError(err); setLoading(false); }
    );
  }, [primaryState]);

  useEffect(() => { fetch_(); }, [fetch_]);

  if (!primaryState) {
    return (
      <section className={styles.wrap} aria-label="Market data">
        <div className={styles.emptyState}>
          <span className={styles.emptyText}>Select a state to view market data</span>
        </div>
      </section>
    );
  }

  const overview = data?.overview || {};
  const counties = data?.top_counties || [];

  return (
    <section className={styles.wrap} aria-label="Market data">
      <header className={styles.header}>
        <span className={styles.title}>Market Overview</span>
        <span className={styles.stateBadge}>{primaryState}</span>
      </header>

      {loading ? (
        <div className={styles.skeleton} aria-hidden="true">
          <div className={styles.skelStats} />
          <div className={styles.skelTable} />
        </div>
      ) : error ? (
        <div className={styles.errorBox} role="alert">
          <span className={styles.errorText}>
            {error.status === 404 ? 'Market service not deployed.' : error.message}
          </span>
          <button type="button" className={styles.retryBtn} onClick={fetch_}>Retry</button>
        </div>
      ) : (
        <>
          <div className={styles.statsGrid}>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Median Price</span>
              <span className={styles.statValue}>
                {overview.median_price != null ? fmtPrice.format(overview.median_price) : '—'}
              </span>
              <TrendArrow value={overview.median_price_yoy} />
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Days on Market</span>
              <span className={styles.statValue}>
                {overview.median_dom != null ? fmtInt.format(overview.median_dom) : '—'}
              </span>
              <TrendArrow value={overview.dom_yoy} />
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Active Inventory</span>
              <span className={styles.statValue}>
                {overview.active_listings != null ? fmtInt.format(overview.active_listings) : '—'}
              </span>
              <TrendArrow value={overview.inventory_yoy} />
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Sold Last 30d</span>
              <span className={styles.statValue}>
                {overview.sold_30d != null ? fmtInt.format(overview.sold_30d) : '—'}
              </span>
            </div>
          </div>

          {counties.length > 0 && (
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>County</th>
                    <th>Median</th>
                    <th>Vol</th>
                    <th>DOM</th>
                  </tr>
                </thead>
                <tbody>
                  {counties.slice(0, 8).map((c) => (
                    <tr key={c.fips_code || c.county_name}>
                      <td className={styles.countyName}>{c.county_name}</td>
                      <td>{c.median_price != null ? fmtPrice.format(c.median_price) : '—'}</td>
                      <td>{c.volume != null ? fmtInt.format(c.volume) : '—'}</td>
                      <td>{c.median_dom != null ? fmtInt.format(c.median_dom) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}
