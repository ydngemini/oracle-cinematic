import { useCallback, useEffect, useState } from 'react';
import { Building2, RefreshCw, Search } from 'lucide-react';
import { crmGet } from '../state/useCrmApi';
import styles from './MlsSearch.module.css';

/**
 * Retail MLS browse over authorized, already-ingested listing rows.
 *
 * Distinct from the Houses view, which browses `/api/mls/public-records` — the
 * shared public parcel catalogue. This is `oracle_mls_listings`: direct
 * MLS/RESO rows the workspace is licensed to see. Both had detail endpoints
 * with no caller, so a record could be listed and never opened.
 *
 * The board-coverage strip is here rather than in a settings screen because an
 * empty result is ambiguous without it: no listings in a state can mean the
 * market is quiet, or that no board covering that state has ever been ingested.
 * Those need to look different.
 */

const money = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', maximumFractionDigits: 0,
});

function priceOf(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? money.format(parsed) : 'No price';
}

function specsOf(listing) {
  const parts = [];
  if (listing.beds != null) parts.push(`${listing.beds} bd`);
  const baths = (Number(listing.baths_full) || 0) + (Number(listing.baths_half) || 0) * 0.5;
  if (baths > 0) parts.push(`${baths} ba`);
  if (listing.sqft) parts.push(`${Number(listing.sqft).toLocaleString()} sqft`);
  if (listing.year_built) parts.push(`built ${listing.year_built}`);
  return parts;
}

function ListingDetail({ listingId, onClose }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let alive = true;
    crmGet(`/api/mls/listings/${encodeURIComponent(listingId)}`).then(
      (payload) => { if (alive) setDetail(payload?.listing || payload || null); },
      (reason) => { if (alive) setError(reason?.message || 'This listing could not be opened.'); },
    );
    return () => { alive = false; };
  }, [listingId]);

  const listing = detail || {};
  return (
    <div className={styles.detail}>
      <div className={styles.detailHead}>
        <strong>{listing.address || 'Listing detail'}</strong>
        <button type="button" onClick={onClose}>Close</button>
      </div>
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      {!detail && !error ? <p className={styles.muted}>Loading…</p> : null}
      {detail ? (
        <dl className={styles.detailGrid}>
          <div><dt>MLS number</dt><dd>{listing.mls_number || '—'}</dd></div>
          <div><dt>Status</dt><dd>{listing.status || '—'}</dd></div>
          <div><dt>List price</dt><dd>{priceOf(listing.list_price)}</dd></div>
          <div><dt>Original</dt><dd>{priceOf(listing.orig_list_price)}</dd></div>
          <div><dt>Days on market</dt><dd>{listing.days_on_market ?? '—'}</dd></div>
          <div><dt>Lot</dt><dd>{listing.lot_sqft ? `${Number(listing.lot_sqft).toLocaleString()} sqft` : '—'}</dd></div>
          <div><dt>HOA</dt><dd>{listing.hoa_monthly ? `${priceOf(listing.hoa_monthly)}/mo` : 'None recorded'}</dd></div>
          <div><dt>County</dt><dd>{listing.county || '—'}</dd></div>
        </dl>
      ) : null}
    </div>
  );
}

export default function MlsSearch() {
  const [filters, setFilters] = useState({ state: '', city: '', zip: '', min_price: '', max_price: '', beds: '' });
  const [result, setResult] = useState(null);
  const [regions, setRegions] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [openId, setOpenId] = useState(null);

  const search = useCallback((page = 1) => {
    setBusy(true);
    setError('');
    const params = new URLSearchParams({ page: String(page) });
    Object.entries(filters).forEach(([key, value]) => {
      const trimmed = String(value).trim();
      if (trimmed) params.set(key, key === 'state' ? trimmed.toUpperCase() : trimmed);
    });
    return crmGet(`/api/mls/search?${params.toString()}`).then(
      (payload) => { setResult(payload || null); setBusy(false); },
      (reason) => {
        setError(reason?.message || 'The MLS cache did not answer.');
        setBusy(false);
      },
    );
  }, [filters]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void search(1); });
    return () => window.cancelAnimationFrame(frame);
    // Deliberately first-load only: typing in a filter should not fire a query
    // per keystroke against a quota-bounded cache.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let alive = true;
    crmGet('/api/mls/regions').then(
      (payload) => { if (alive) setRegions(Array.isArray(payload) ? payload : payload?.regions || []); },
      () => { if (alive) setRegions([]); },
    );
    return () => { alive = false; };
  }, []);

  const listings = Array.isArray(result?.listings) ? result.listings : [];
  const set = (key) => (event) => setFilters((prev) => ({ ...prev, [key]: event.target.value }));

  return (
    <section className={styles.wrap} aria-labelledby="mls-title">
      <header className={styles.head}>
        <div>
          <h2 id="mls-title">MLS search</h2>
          <p>
            Authorized listing rows already ingested into this workspace. Distinct from Houses,
            which browses the shared public parcel catalogue.
          </p>
        </div>
        <button type="button" onClick={() => search(1)} disabled={busy} aria-label="Run search">
          <RefreshCw aria-hidden="true" />
        </button>
      </header>

      <div className={styles.filters}>
        <label><span>State</span><input value={filters.state} onChange={set('state')} maxLength={2} placeholder="DE" /></label>
        <label><span>City</span><input value={filters.city} onChange={set('city')} maxLength={120} /></label>
        <label><span>ZIP</span><input value={filters.zip} onChange={set('zip')} maxLength={10} /></label>
        <label><span>Min price</span><input value={filters.min_price} onChange={set('min_price')} inputMode="numeric" /></label>
        <label><span>Max price</span><input value={filters.max_price} onChange={set('max_price')} inputMode="numeric" /></label>
        <label><span>Beds</span><input value={filters.beds} onChange={set('beds')} inputMode="numeric" /></label>
        <button type="button" onClick={() => search(1)} disabled={busy}>
          <Search aria-hidden="true" /> {busy ? 'Searching…' : 'Search'}
        </button>
      </div>

      {error ? <p className={styles.error} role="alert">{error}</p> : null}

      {regions !== null ? (
        <p className={styles.muted}>
          {regions.length === 0
            ? 'No MLS board coverage is configured, so this cache can only be empty — an empty result here is not a quiet market.'
            : `${regions.length} board${regions.length === 1 ? '' : 's'} configured: ${regions.slice(0, 6).map((r) => r.mls_id || r.name).filter(Boolean).join(', ')}${regions.length > 6 ? '…' : ''}`}
        </p>
      ) : null}

      {result ? (
        <p className={styles.muted}>
          {result.total ?? listings.length} matching · page {result.page ?? 1}
          {result.source ? ` · ${result.source}` : ''}
          {result.degraded ? ' · DEGRADED' : ''}
        </p>
      ) : null}
      {result?.notice ? <p className={styles.muted}>{result.notice}</p> : null}

      {listings.length === 0 && result && !busy ? (
        <div className={styles.empty}>
          <Building2 aria-hidden="true" />
          <p>No listings matched.</p>
        </div>
      ) : null}

      <ul className={styles.list}>
        {listings.map((listing) => (
          <li key={listing.id}>
            <div>
              <strong>{listing.address}</strong>
              <small>
                {[listing.city, listing.state_code, listing.zip_code].filter(Boolean).join(', ')}
                {specsOf(listing).length ? ` · ${specsOf(listing).join(' · ')}` : ''}
              </small>
            </div>
            <span>{priceOf(listing.list_price)}</span>
            <button
              type="button"
              onClick={() => setOpenId((current) => (current === listing.id ? null : listing.id))}
              aria-expanded={openId === listing.id}
            >
              {openId === listing.id ? 'Close' : 'Detail'}
            </button>
            {openId === listing.id ? (
              <ListingDetail listingId={listing.id} onClose={() => setOpenId(null)} />
            ) : null}
          </li>
        ))}
      </ul>

      {result?.has_more ? (
        <button
          type="button"
          className={styles.more}
          onClick={() => search((result.page || 1) + 1)}
          disabled={busy}
        >
          Next page
        </button>
      ) : null}
    </section>
  );
}
