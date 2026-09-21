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
};

const fmtPrice = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const fmtPct = new Intl.NumberFormat('en-US', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 });
const fmtInt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const fmtRatio = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });

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

function num(value) {
  // Number(null) and Number('') are both 0 — treat absent as absent, not zero,
  // so a deliberately-NULL column shows a dash rather than "$0".
  if (value == null || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * State-level market overview.
 *
 * Reads `GET /api/market/{state}/overview`, whose response is the flat
 * `StateMarketOverview` shape from `backend/state_compliance/routes_market.py`:
 * median list/sale price, days on market, months of supply, active listings,
 * closed sales, price/sqft, plus `as_of_date` / `is_stale` / `data_vintage_days`
 * so the numbers can be shown with their vintage rather than as if current.
 *
 * `state_market_stats` is refreshed from the scheduled Redfin sync
 * (`state_market_projection`). The publisher lag is ~3 months, so a freshly
 * projected row is still ~80 days old — inside the stale threshold, but the
 * banner names the vintage regardless. Several YoY / ratio columns are
 * deliberately NULL on a projected row (the harvested source answers a
 * different question); those tiles fall back to a dash rather than a zero.
 */
export function MarketDataPanels() {
  const { primaryState } = useStateCtx();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    if (!primaryState) return;
    crmGet(`/api/market/${primaryState}/overview`).then(
      (res) => { setData(res); setLoading(false); setError(null); },
      (err) => { setError(err); setLoading(false); },
    );
  }, [primaryState]);

  // Show the spinner again when the state changes — render-phase reset keeps the
  // fetch effect free of synchronous setState (set-state-in-effect).
  const mdKey = primaryState || '';
  const [prevMdKey, setPrevMdKey] = useState(mdKey);
  if (mdKey !== prevMdKey) { setPrevMdKey(mdKey); setLoading(Boolean(primaryState)); setError(null); }

  useEffect(() => { load(); }, [load]);

  if (!primaryState) {
    return (
      <section className={styles.wrap} aria-label="Market data">
        <div className={styles.emptyState}>
          <span className={styles.emptyText}>Select a state to view market data</span>
        </div>
      </section>
    );
  }

  const medianPrice = num(data?.median_sale_price) ?? num(data?.median_list_price);
  const priceLabel = data?.median_sale_price != null ? 'Median Sale Price' : 'Median List Price';
  const dom = num(data?.median_days_on_market);
  const supply = num(data?.months_of_supply);
  const active = num(data?.active_listings);
  const sold30 = num(data?.closed_sales_last_30d);
  const perSqft = num(data?.avg_price_per_sqft);
  const priceYoy = num(data?.yoy_price_change_pct);
  const vintageDays = num(data?.data_vintage_days);

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
            {error.status === 404
              ? `No market data has been loaded for ${primaryState}.`
              : (error.message || 'Market data could not be read.')}
          </span>
          <button type="button" className={styles.retryBtn} onClick={load}>Retry</button>
        </div>
      ) : (
        <>
          <div className={styles.statsGrid}>
            <div className={styles.stat}>
              <span className={styles.statLabel}>{priceLabel}</span>
              <span className={styles.statValue}>
                {medianPrice != null ? fmtPrice.format(medianPrice) : '—'}
              </span>
              <TrendArrow value={priceYoy} />
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Days on Market</span>
              <span className={styles.statValue}>
                {dom != null ? fmtInt.format(dom) : '—'}
              </span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Active Inventory</span>
              <span className={styles.statValue}>
                {active != null ? fmtInt.format(active) : '—'}
              </span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Months of Supply</span>
              <span className={styles.statValue}>
                {supply != null ? fmtRatio.format(supply) : '—'}
              </span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Sold Last 30d</span>
              <span className={styles.statValue}>
                {sold30 != null ? fmtInt.format(sold30) : '—'}
              </span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Price / sq ft</span>
              <span className={styles.statValue}>
                {perSqft != null ? fmtPrice.format(perSqft) : '—'}
              </span>
            </div>
          </div>

          {data?.as_of_date ? (
            <p className={styles.emptyText}>
              Reflects the {data.as_of_date} reporting period
              {vintageDays != null ? ` · ~${vintageDays} days old` : ''}
              {data.is_stale ? ' · verify before pricing against these figures' : ''}
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}
