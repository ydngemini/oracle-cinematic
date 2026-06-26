import { useCallback, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import ListingDetail from './ListingDetail';
import styles from './MarketplaceBrowse.module.css';

// Inline stroke glyphs — currentColor, zero icon deps (house rule).
const GLYPHS = {
  house: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20h13V9.5" />
      <path d="M9.5 20v-6h5v6" />
    </svg>
  ),
  search: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.2-3.2" />
    </svg>
  ),
};

const US_STATES = [
  'AL','AK','AZ','AR','CA','CO','CT','DE','DC','FL','GA','HI','ID','IL','IN','IA',
  'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM',
  'NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA',
  'WV','WI','WY',
];

const PROPERTY_TYPES = [
  { label: 'Any type', value: '' },
  { label: 'Single Family', value: 'Single Family' },
  { label: 'Condo', value: 'Condo' },
  { label: 'Townhouse', value: 'Townhouse' },
  { label: 'Multi-Family', value: 'Multi-Family' },
  { label: 'Land', value: 'Land' },
];

const BEDS_OPTS = [
  { label: 'Any beds', value: '' },
  { label: '1+ bd', value: '1' },
  { label: '2+ bd', value: '2' },
  { label: '3+ bd', value: '3' },
  { label: '4+ bd', value: '4' },
  { label: '5+ bd', value: '5' },
];

const fmtInt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

function toNum(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = typeof v === 'string' ? Number(v) : v;
  return Number.isFinite(n) ? n : null;
}

function formatPrice(v) {
  const n = toNum(v);
  return n === null || n <= 0 ? null : `$${fmtInt.format(n)}`;
}

function formatBaths(n) {
  if (n === null) return null;
  return n % 1 === 0 ? fmtInt.format(n) : n.toFixed(1);
}

function normalizeStatus(s) {
  const t = String(s || '').toLowerCase();
  if (t.startsWith('active')) return 'active';
  if (t.startsWith('pend')) return 'pending';
  if (t.startsWith('sold') || t.startsWith('clos')) return 'sold';
  return 'other';
}

/**
 * MarketplaceBrowse — Neoh's retail MLS discovery surface.
 *
 * Searches an area (city/state/zip), filters locally-served results, and renders
 * a responsive card grid. All data comes from GET /api/mls/<...>, which serves
 * RentCast listings cached into oracle_mls_listings (quota-safe — one provider
 * fetch per area per day). Renders only real rows; never simulates a listing.
 */
export default function MarketplaceBrowse() {
  const [form, setForm] = useState({
    city: '',
    state: '',
    zip: '',
    minPrice: '',
    maxPrice: '',
    beds: '',
    propertyType: '',
  });
  const [result, setResult] = useState(null); // null = no search yet
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState(null); // listing id for detail view

  const setField = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const runSearch = useCallback((nextPage) => {
    const f = form;
    const params = new URLSearchParams();
    if (f.city.trim()) params.set('city', f.city.trim());
    if (f.state.trim()) params.set('state', f.state.trim());
    if (f.zip.trim()) params.set('zip', f.zip.trim());
    if (f.minPrice) params.set('min_price', String(toNum(f.minPrice) ?? ''));
    if (f.maxPrice) params.set('max_price', String(toNum(f.maxPrice) ?? ''));
    if (f.beds) params.set('beds', f.beds);
    if (f.propertyType) params.set('property_type', f.propertyType);
    params.set('page', String(nextPage));

    setLoading(true);
    setError(null);
    return crmGet(`/api/mls/search?${params.toString()}`).then(
      (data) => {
        setResult(data);
        setPage(nextPage);
        setLoading(false);
      },
      (err) => {
        setError(err);
        setLoading(false);
      }
    );
  }, [form]);

  const onSubmit = (e) => {
    e.preventDefault();
    runSearch(1);
  };

  const hasArea = form.city.trim() || form.state.trim() || form.zip.trim();
  const listings = result?.listings ?? [];
  const total = result?.total ?? 0;
  const errMsg =
    error?.status === 404
      ? 'Listings service isn’t online yet.'
      : error?.message || 'Couldn’t reach the listings service.';

  if (selected) {
    return <ListingDetail listingId={selected} onBack={() => setSelected(null)} />;
  }

  return (
    <section className={styles.wrap} aria-label="Neoh — browse listings">
      <header className={styles.head}>
        <span className={styles.brandRow}>
          <span className={styles.kicker}>Neoh</span>
          <span className={styles.subKicker}>Browse the market</span>
        </span>
      </header>

      <form className={styles.searchForm} onSubmit={onSubmit}>
        <div className={styles.areaRow}>
          <label className={styles.cityField}>
            <span className={styles.srOnly}>City</span>
            <input
              className={styles.input}
              type="text"
              inputMode="text"
              placeholder="City (e.g. Wilmington)"
              value={form.city}
              onChange={setField('city')}
              autoCapitalize="words"
            />
          </label>
          <label className={styles.stateField}>
            <span className={styles.srOnly}>State</span>
            <select className={styles.select} value={form.state} onChange={setField('state')}>
              <option value="">State</option>
              {US_STATES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
          <label className={styles.zipField}>
            <span className={styles.srOnly}>ZIP code</span>
            <input
              className={styles.input}
              type="text"
              inputMode="numeric"
              pattern="[0-9]*"
              maxLength={5}
              placeholder="ZIP"
              value={form.zip}
              onChange={setField('zip')}
            />
          </label>
          <button className={styles.searchBtn} type="submit" disabled={loading} aria-label="Search listings">
            {GLYPHS.search}
            <span className={styles.searchBtnLabel}>Search</span>
          </button>
        </div>

        <div className={styles.filterRow}>
          <label className={styles.priceField}>
            <span className={styles.srOnly}>Minimum price</span>
            <input
              className={styles.input}
              type="number"
              min="0"
              step="1000"
              inputMode="numeric"
              placeholder="Min $"
              value={form.minPrice}
              onChange={setField('minPrice')}
            />
          </label>
          <span className={styles.priceDash} aria-hidden="true">–</span>
          <label className={styles.priceField}>
            <span className={styles.srOnly}>Maximum price</span>
            <input
              className={styles.input}
              type="number"
              min="0"
              step="1000"
              inputMode="numeric"
              placeholder="Max $"
              value={form.maxPrice}
              onChange={setField('maxPrice')}
            />
          </label>
          <label className={styles.selectField}>
            <span className={styles.srOnly}>Beds</span>
            <select className={styles.select} value={form.beds} onChange={setField('beds')}>
              {BEDS_OPTS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </label>
          <label className={styles.selectField}>
            <span className={styles.srOnly}>Property type</span>
            <select className={styles.select} value={form.propertyType} onChange={setField('propertyType')}>
              {PROPERTY_TYPES.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </label>
        </div>
      </form>

      {/* Quota-honest provenance line — always visible once a search has run. */}
      {result && (
        <div className={styles.metaRow}>
          <span className={styles.sourceNote}>Data via RentCast, cached</span>
          {result.degraded && (
            <span className={styles.degradedNote} role="status">
              {result.notice || 'Live feed unavailable — showing cached listings.'}
            </span>
          )}
        </div>
      )}

      {loading ? (
        <div className={styles.grid} aria-hidden="true">
          <div className={styles.skelCard} />
          <div className={styles.skelCard} />
          <div className={styles.skelCard} />
          <div className={styles.skelCard} />
        </div>
      ) : error ? (
        <div className={styles.stateBox} role="alert">
          <span className={styles.errorTick} aria-hidden="true" />
          <p className={styles.stateText}>{errMsg}</p>
          <button type="button" className={styles.retryBtn} onClick={() => runSearch(page)}>
            Retry
          </button>
        </div>
      ) : result === null ? (
        <div className={styles.stateBox}>
          <span className={styles.stateGlyph} aria-hidden="true">{GLYPHS.house}</span>
          <p className={styles.stateTitle}>Search a city to load listings</p>
          <p className={styles.stateHint}>
            Enter a city + state or a ZIP, then hit Search. Neoh pulls the market once and serves it instantly after.
          </p>
        </div>
      ) : listings.length === 0 ? (
        <div className={styles.stateBox}>
          <span className={styles.stateGlyph} aria-hidden="true">{GLYPHS.house}</span>
          <p className={styles.stateTitle}>
            {hasArea ? 'No listings found for this area' : 'Enter an area to search'}
          </p>
          <p className={styles.stateHint}>
            {result.degraded
              ? 'The live feed is unavailable right now and nothing is cached for this area yet.'
              : 'Try a broader area or different filters.'}
          </p>
        </div>
      ) : (
        <>
          <div className={styles.resultMeta}>
            <span className={styles.resultCount}>{fmtInt.format(total)} {total === 1 ? 'listing' : 'listings'}</span>
          </div>

          <ul className={styles.grid} role="list">
            {listings.map((l, i) => {
              const status = normalizeStatus(l.status);
              const price = formatPrice(l.price);
              const beds = toNum(l.beds);
              const baths = toNum(l.baths);
              const sqft = toNum(l.sqft);
              return (
                <li
                  key={l.id ?? `${l.address}-${i}`}
                  className={styles.card}
                  style={{ animationDelay: `${Math.min(i, 8) * 45}ms` }}
                >
                  <button
                    type="button"
                    className={styles.cardBtn}
                    onClick={() => setSelected(l.id)}
                    aria-label={`View ${l.address || 'listing'}`}
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
                        {status === 'other' ? (l.status || '—') : status}
                      </span>
                    </div>
                    <div className={styles.body}>
                      {price ? (
                        <span className={styles.price}>{price}</span>
                      ) : (
                        <span className={styles.priceTbd}>Price TBD</span>
                      )}
                      <span className={styles.address}>{l.address || 'Address pending'}</span>
                      <span className={styles.locale}>
                        {[l.city, l.state].filter(Boolean).join(', ')}{l.zip ? ` ${l.zip}` : ''}
                      </span>
                      {(beds !== null || baths !== null || sqft !== null) && (
                        <div className={styles.chips}>
                          {beds !== null && <span className={styles.chip}>{fmtInt.format(beds)} bd</span>}
                          {baths !== null && <span className={styles.chip}>{formatBaths(baths)} ba</span>}
                          {sqft !== null && sqft > 0 && <span className={styles.chip}>{fmtInt.format(sqft)} sqft</span>}
                        </div>
                      )}
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>

          {(page > 1 || result.has_more) && (
            <nav className={styles.pager} aria-label="Pagination">
              <button
                type="button"
                className={styles.pagerBtn}
                onClick={() => runSearch(page - 1)}
                disabled={page <= 1 || loading}
              >
                Prev
              </button>
              <span className={styles.pagerPage}>Page {page}</span>
              <button
                type="button"
                className={styles.pagerBtn}
                onClick={() => runSearch(page + 1)}
                disabled={!result.has_more || loading}
              >
                Next
              </button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}
