import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { MarketDataPanels } from './MarketDataPanels';
import styles from './MarketplaceTab.module.css';

// Inline stroke glyphs — currentColor, zero icon deps (house rule).
const GLYPHS = {
  house: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20h13V9.5" />
      <path d="M9.5 20v-6h5v6" />
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
const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

// numeric(12,2) may arrive as a string — parse defensively, never trust shape.
function toNum(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = typeof v === 'string' ? Number(v) : v;
  return Number.isFinite(n) ? n : null;
}

function formatPrice(v) {
  const n = toNum(v);
  return n === null ? null : `$${fmtInt.format(n)}`;
}

// $1.2M / $850K short-form for the stat strip.
function shortDollars(n) {
  const trim = (x) => {
    const s = x >= 100 ? String(Math.round(x)) : x.toFixed(1);
    return s.replace(/\.0$/, '');
  };
  const abs = Math.abs(n);
  if (abs >= 1e9) return `$${trim(n / 1e9)}B`;
  if (abs >= 1e6) return `$${trim(n / 1e6)}M`;
  if (abs >= 1e3) return `$${trim(n / 1e3)}K`;
  return `$${fmtInt.format(n)}`;
}

function formatBaths(n) {
  return n % 1 === 0 ? fmtInt.format(n) : n.toFixed(1);
}

function formatListedDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return `${MONTHS[d.getMonth()]} ${d.getDate()} ’${String(d.getFullYear()).slice(-2)}`;
}

function initialsOf(name) {
  return (
    name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((w) => w[0].toUpperCase())
      .join('') || '·'
  );
}

function normalizeStatus(s) {
  const t = String(s || '').toLowerCase();
  return t === 'active' || t === 'pending' || t === 'sold' ? t : 'other';
}

/**
 * MarketplaceTab — the landing view: the agent's active listings.
 * Renders only what GET /api/crm/listings returns; never simulates data.
 * Every card wears its owner chip — the property↔person graph made visible.
 * A listing with no seller linked is a quiet warning: the AI cannot email
 * the owner until the agent links one (House tab).
 */
export default function MarketplaceTab() {
  const [listings, setListings] = useState(null); // null = not yet loaded
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  // All setState lands in async continuations — never synchronously inside
  // the effect's call stack (react-hooks/set-state-in-effect compliant).
  const fetchListings = useCallback(() => {
    return crmGet('/api/crm/listings').then(
      (data) => {
        setListings(Array.isArray(data?.listings) ? data.listings : []);
        setError(null);
        setRefreshing(false);
      },
      (err) => {
        setError(err); // 404 until the route deploys — handled below, never crash
        setRefreshing(false);
      }
    );
  }, []);

  useEffect(() => {
    fetchListings();
  }, [fetchListings]);

  const refresh = () => {
    setRefreshing(true);
    setError(null);
    fetchListings();
  };

  const retry = () => {
    setError(null); // back to skeletons while the retry is in flight
    fetchListings();
  };

  const items = listings ?? [];
  const activeCount = items.filter((l) => normalizeStatus(l.status) === 'active').length;
  const pendingCount = items.filter((l) => normalizeStatus(l.status) === 'pending').length;
  const combined = items.reduce((sum, l) => {
    if (normalizeStatus(l.status) === 'sold') return sum;
    const n = toNum(l.price);
    return n === null ? sum : sum + n;
  }, 0);

  const isLoading = listings === null && !error;
  const errMsg =
    error?.status === 404
      ? 'Listings service isn’t online yet.'
      : error?.message || 'Couldn’t reach the listings service.';

  return (
    <section className={styles.wrap} aria-label="Marketplace — active listings" aria-busy={isLoading || refreshing}>
      <header className={styles.topRow}>
        <span className={styles.kicker}>Marketplace</span>
        <button
          type="button"
          className={`${styles.refreshBtn} ${refreshing ? styles.refreshing : ''}`}
          onClick={refresh}
          disabled={refreshing || isLoading}
          aria-label="Refresh listings"
        >
          {GLYPHS.refresh}
        </button>
      </header>

      <MarketDataPanels />

      {isLoading ? (
        <div className={styles.skeleton} aria-hidden="true">
          <div className={styles.skelStrip} />
          <div className={styles.skelCard} />
          <div className={styles.skelCard} />
          <div className={styles.skelCard} />
        </div>
      ) : error && listings === null ? (
        <div className={styles.errorBox} role="alert">
          <span className={styles.errorTick} aria-hidden="true" />
          <p className={styles.errorText}>{errMsg}</p>
          <button type="button" className={styles.retryBtn} onClick={retry}>
            Retry
          </button>
        </div>
      ) : (
        <>
          <dl className={styles.stats}>
            <div className={styles.stat}>
              <dt className={styles.statLabel}>Active</dt>
              <dd className={styles.statValue}>{fmtInt.format(activeCount)}</dd>
            </div>
            <div className={styles.stat}>
              <dt className={styles.statLabel}>Combined</dt>
              <dd className={`${styles.statValue} ${styles.statAmber}`}>
                {combined > 0 ? shortDollars(combined) : '—'}
              </dd>
            </div>
            <div className={styles.stat}>
              <dt className={styles.statLabel}>Pending</dt>
              <dd className={styles.statValue}>{fmtInt.format(pendingCount)}</dd>
            </div>
          </dl>

          {error && (
            <div className={styles.errorStrip} role="alert">
              <span className={styles.errorTick} aria-hidden="true" />
              <p className={styles.errorText}>{errMsg}</p>
              <button type="button" className={styles.retrySm} onClick={refresh}>
                Retry
              </button>
            </div>
          )}

          {items.length === 0 ? (
            <div className={styles.empty}>
              <span className={styles.emptyGlyph} aria-hidden="true">{GLYPHS.house}</span>
              <p className={styles.emptyTitle}>No active listings yet</p>
              <p className={styles.emptyHint}>
                Stage a property from the House tab and it will appear here.
              </p>
            </div>
          ) : (
            <ul className={styles.list} role="list">
              {items.map((l, i) => {
                const status = normalizeStatus(l.status);
                const price = formatPrice(l.price);
                const beds = toNum(l.beds);
                const baths = toNum(l.baths);
                const sqft = toNum(l.sqft);
                const ownerName = l.seller?.full_name?.trim() || '';
                const listed = formatListedDate(l.created_at);

                return (
                  <li
                    key={l.id ?? `${l.address}-${i}`}
                    className={styles.card}
                    style={{ animationDelay: `${Math.min(i, 8) * 70}ms` }}
                  >
                    <div className={styles.cover}>
                      <span className={styles.coverGhost} aria-hidden="true">{GLYPHS.house}</span>
                      {l.cover_url && (
                        <img
                          className={styles.coverImg}
                          src={l.cover_url}
                          alt=""
                          loading="lazy"
                          onError={(e) => { e.currentTarget.hidden = true; }}
                        />
                      )}
                      <span className={styles.pill} data-status={status}>
                        {status === 'other' ? l.status || '—' : status}
                      </span>
                    </div>

                    <div className={styles.body}>
                      {price ? (
                        <span className={styles.price}>{price}</span>
                      ) : (
                        <span className={styles.priceTbd}>Price TBD</span>
                      )}
                      <span className={styles.address}>{l.address || 'Address pending'}</span>
                      {(beds !== null || baths !== null || sqft !== null) && (
                        <div className={styles.chips}>
                          {beds !== null && <span className={styles.chip}>{fmtInt.format(beds)} bd</span>}
                          {baths !== null && <span className={styles.chip}>{formatBaths(baths)} ba</span>}
                          {sqft !== null && <span className={styles.chip}>{fmtInt.format(sqft)} sqft</span>}
                        </div>
                      )}
                    </div>

                    <footer className={styles.cardFoot}>
                      {ownerName ? (
                        <span className={styles.owner}>
                          <span className={styles.ownerRing} aria-hidden="true">
                            {initialsOf(ownerName)}
                          </span>
                          <span className={styles.ownerName}>{ownerName}</span>
                        </span>
                      ) : (
                        <span
                          className={styles.owner}
                          title="Link a seller in the House tab so the AI can email the owner."
                        >
                          <span className={styles.ownerRingGhost} aria-hidden="true">·</span>
                          <span className={styles.ownerGhostLabel}>No owner linked</span>
                        </span>
                      )}
                      {listed && <span className={styles.listedAt}>Listed {listed}</span>}
                    </footer>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
