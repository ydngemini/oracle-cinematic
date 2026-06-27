import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { locationFor, usePropertyCover } from '../lib/propertyImagery';
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

// Human labels for harvested distress signals (payload.distress_flags).
const FLAG_LABELS = {
  absentee_owner: 'Absentee',
  tax_exempt: 'Tax Exempt',
  tax_delinquent: 'Tax Delinquent',
  high_equity: 'High Equity',
  vacant: 'Vacant',
  pre_foreclosure: 'Pre-Foreclosure',
  foreclosure: 'Foreclosure',
  probate: 'Probate',
  out_of_state: 'Out of State',
  free_and_clear: 'Free & Clear',
  long_tenure: 'Long Tenure',
};

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

function flagLabel(f) {
  if (FLAG_LABELS[f]) return FLAG_LABELS[f];
  return String(f || '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// Motivation tier drives the meter colour (0–100 distress/urgency score).
function motivationTier(score) {
  const n = toNum(score) ?? 0;
  if (n >= 60) return 'high';
  if (n >= 35) return 'mid';
  return 'low';
}

function clampPct(score) {
  const n = toNum(score) ?? 0;
  return Math.max(0, Math.min(100, n));
}

/**
 * MarketplaceBrowse — Neoh's listing discovery surface.
 *
 * Two real sources, switchable with a toggle:
 *   • Pipeline (default) — the 240k+ REAL harvested leads, served from the
 *     `leads` table via GET /api/mls/pipeline. Cards wear distress badges
 *     (absentee, tax-exempt, …) and a motivation meter. This is where the data
 *     actually lives today, so it loads on mount.
 *   • MLS — RentCast-cached retail listings via GET /api/mls/search (empty until
 *     a feed/key is live; kept here so it lights up automatically when it is).
 *
 * Renders only real rows; never simulates a listing and never invents a price —
 * an unpriced lead shows "Unpriced", not a fabricated number.
 */
export default function MarketplaceBrowse() {
  const [source, setSource] = useState('pipeline'); // 'pipeline' | 'mls'
  const [form, setForm] = useState({
    city: '',
    state: '',
    zip: '',
    minPrice: '',
    maxPrice: '',
    beds: '',
    propertyType: '',
    keyword: '',
  });
  const [result, setResult] = useState(null); // null = no search yet
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState(null); // listing id for detail view

  const setField = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const runSearch = useCallback((nextPage, src) => {
    const useSource = src || source;
    const f = form;
    const params = new URLSearchParams();
    if (f.city.trim()) params.set('city', f.city.trim());
    if (f.state.trim()) params.set('state', f.state.trim());
    if (f.zip.trim()) params.set('zip', f.zip.trim());
    if (f.minPrice) params.set('min_price', String(toNum(f.minPrice) ?? ''));
    if (f.maxPrice) params.set('max_price', String(toNum(f.maxPrice) ?? ''));
    if (f.beds) params.set('beds', f.beds);
    if (useSource === 'pipeline') {
      if (f.keyword.trim()) params.set('q', f.keyword.trim());
    } else if (f.propertyType) {
      params.set('property_type', f.propertyType);
    }
    params.set('page', String(nextPage));

    const endpoint = useSource === 'pipeline' ? '/api/mls/pipeline' : '/api/mls/search';
    setLoading(true);
    setError(null);
    return crmGet(`${endpoint}?${params.toString()}`).then(
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
  }, [form, source]);

  // Pipeline is the default and holds the real harvested inventory — load it
  // immediately so the Listings tab is never empty on arrival.
  useEffect(() => {
    // Mount-time initial load. runSearch sets loading/error synchronously and is
    // shared by 5 event handlers (pagination, retry, source toggle), so the
    // loading flag stays in runSearch; the mount fetch here is intentional.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    runSearch(1, 'pipeline');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const switchSource = (next) => {
    if (next === source) return;
    setSource(next);
    setSelected(null);
    setError(null);
    setPage(1);
    if (next === 'pipeline') {
      runSearch(1, 'pipeline'); // pipeline browses with or without filters
    } else {
      setResult(null); // MLS needs an explicit area search
    }
  };

  const onSubmit = (e) => {
    e.preventDefault();
    runSearch(1);
  };

  const isPipeline = source === 'pipeline';
  const hasArea = form.city.trim() || form.state.trim() || form.zip.trim();
  const listings = result?.listings ?? [];
  const total = result?.total ?? 0;
  const errMsg =
    error?.status === 404
      ? 'Listings service isn’t online yet.'
      : error?.message || 'Couldn’t reach the listings service.';

  if (selected) {
    return (
      <ListingDetail
        listingId={selected}
        source={source}
        onBack={() => setSelected(null)}
      />
    );
  }

  return (
    <section className={styles.wrap} aria-label="Neoh — browse listings">
      <header className={styles.head}>
        <span className={styles.brandRow}>
          <span className={styles.kicker}>Neoh</span>
          <span className={styles.subKicker}>
            {isPipeline ? 'Acquisition pipeline' : 'Browse the market'}
          </span>
        </span>
        <div className={styles.sourceToggle} role="tablist" aria-label="Listing source">
          <button
            type="button"
            role="tab"
            aria-selected={isPipeline}
            className={styles.sourceBtn}
            data-active={isPipeline}
            onClick={() => switchSource('pipeline')}
          >
            Pipeline
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={!isPipeline}
            className={styles.sourceBtn}
            data-active={!isPipeline}
            onClick={() => switchSource('mls')}
          >
            MLS
          </button>
        </div>
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
          {isPipeline ? (
            <label className={styles.selectField}>
              <span className={styles.srOnly}>Keyword (owner / address)</span>
              <input
                className={styles.input}
                type="text"
                inputMode="text"
                placeholder="Owner or address"
                value={form.keyword}
                onChange={setField('keyword')}
              />
            </label>
          ) : (
            <label className={styles.selectField}>
              <span className={styles.srOnly}>Property type</span>
              <select className={styles.select} value={form.propertyType} onChange={setField('propertyType')}>
                {PROPERTY_TYPES.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </label>
          )}
        </div>
      </form>

      {/* Honest provenance line — always visible once a search has run. */}
      {result && (
        <div className={styles.metaRow}>
          <span className={styles.sourceNote}>
            {isPipeline ? 'Real harvested pipeline' : 'Data via RentCast, cached'}
          </span>
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
          <p className={styles.stateTitle}>Search a city to load MLS listings</p>
          <p className={styles.stateHint}>
            Enter a city + state or a ZIP, then hit Search. Or switch to Pipeline to browse the live acquisition feed.
          </p>
        </div>
      ) : listings.length === 0 ? (
        <div className={styles.stateBox}>
          <span className={styles.stateGlyph} aria-hidden="true">{GLYPHS.house}</span>
          <p className={styles.stateTitle}>
            {isPipeline
              ? 'No pipeline properties match'
              : hasArea
                ? 'No listings found for this area'
                : 'Enter an area to search'}
          </p>
          <p className={styles.stateHint}>
            {isPipeline
              ? 'Loosen the filters — clear the state or price range to see more of the harvested feed.'
              : result.degraded
                ? 'The live feed is unavailable right now and nothing is cached for this area yet.'
                : 'Try a broader area or different filters.'}
          </p>
        </div>
      ) : (
        <>
          <div className={styles.resultMeta}>
            <span className={styles.resultCount}>
              {fmtInt.format(total)} {total === 1 ? 'property' : 'properties'}
            </span>
          </div>

          <ul className={styles.grid} role="list">
            {listings.map((l, i) =>
              isPipeline ? (
                <PipelineCard
                  key={l.id ?? `${l.parcel_id}-${i}`}
                  l={l}
                  i={i}
                  onOpen={() => setSelected(l.id)}
                />
              ) : (
                <MlsCard
                  key={l.id ?? `${l.address}-${i}`}
                  l={l}
                  i={i}
                  onOpen={() => setSelected(l.id)}
                />
              )
            )}
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

// ── cover (real Google imagery, scrim + overlay) ─────────────────────────────

/**
 * PropertyCover — resolves real Street View (→ satellite static → ghost) for a
 * listing and paints it under a gradient scrim. Lazy-loaded, shimmer while it
 * decodes, and an onError chain that walks candidates before degrading to the
 * ghost glyph — never a broken image. `badge` / `pill` sit in the top corners;
 * `footer` overlays the bottom scrim (chips, meter).
 */
function PropertyCover({ location, alt, badge, pill, footer }) {
  const { ready, candidates } = usePropertyCover(location, {
    size: '640x400',
    fov: 80,
  });
  const [idx, setIdx] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [shownFor, setShownFor] = useState(location);

  // Reset the candidate walk + load state whenever the target changes
  // (render-phase reset — no cascading effect).
  if (shownFor !== location) {
    setShownFor(location);
    setIdx(0);
    setLoaded(false);
  }

  const current = candidates[idx];
  const exhausted = ready && (candidates.length === 0 || idx >= candidates.length);
  const showShimmer = !exhausted && (!ready || !loaded);
  // Keep alt honest: the cover can fall back to a satellite tile when no pano
  // exists, so reflect the real source rather than always saying "Street view".
  const imageAlt = current?.kind === 'satellite'
    ? (alt || '').replace(/^Street view of/i, 'Satellite view of')
    : alt;

  return (
    <div className={styles.cover}>
      <span className={styles.coverGhost} aria-hidden="true">{GLYPHS.house}</span>

      {current && (
        <img
          key={current.url}
          className={styles.coverImg}
          data-loaded={loaded ? 'true' : 'false'}
          src={current.url}
          alt={imageAlt}
          loading="lazy"
          decoding="async"
          onLoad={() => setLoaded(true)}
          onError={() => {
            setLoaded(false);
            setIdx((n) => n + 1);
          }}
        />
      )}

      {showShimmer && <span className={styles.coverShimmer} aria-hidden="true" />}
      {current && loaded && <span className={styles.coverScrim} aria-hidden="true" />}

      {badge}
      {pill}
      {footer && <div className={styles.coverFooter}>{footer}</div>}
    </div>
  );
}

// ── cards ────────────────────────────────────────────────────────────────────

function MlsCard({ l, i, onOpen }) {
  const status = normalizeStatus(l.status);
  const price = formatPrice(l.price);
  const beds = toNum(l.beds);
  const baths = toNum(l.baths);
  const sqft = toNum(l.sqft);
  const location = locationFor(l);
  return (
    <li className={styles.card} style={{ animationDelay: `${Math.min(i, 8) * 45}ms` }}>
      <button type="button" className={styles.cardBtn} onClick={onOpen} aria-label={`View ${l.address || 'listing'}`}>
        <PropertyCover
          location={location}
          alt={l.address ? `Street view of ${l.address}` : ''}
          pill={
            <span className={styles.pill} data-status={status}>
              {status === 'other' ? (l.status || '—') : status}
            </span>
          }
        />
        <div className={styles.body}>
          {price ? <span className={styles.price}>{price}</span> : <span className={styles.priceTbd}>Price TBD</span>}
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
}

function PipelineCard({ l, i, onOpen }) {
  const price = formatPrice(l.price);
  const beds = toNum(l.beds);
  const baths = toNum(l.baths);
  const sqft = toNum(l.sqft);
  const flags = Array.isArray(l.distress_flags) ? l.distress_flags : [];
  // Absentee gets its own cover badge; show up to three other signals as
  // scrim chips, with a "+N" tail when there are more.
  const overlayFlags = flags.filter((f) => f !== 'absentee_owner');
  const shownFlags = overlayFlags.slice(0, 3);
  const extraFlags = overlayFlags.length - shownFlags.length;
  const score = toNum(l.motivation_score);
  const location = locationFor(l);

  return (
    <li className={styles.card} style={{ animationDelay: `${Math.min(i, 8) * 45}ms` }}>
      <button type="button" className={styles.cardBtn} onClick={onOpen} aria-label={`View ${l.address || 'property'}`}>
        <PropertyCover
          location={location}
          alt={l.address ? `Street view of ${l.address}` : ''}
          badge={l.is_absentee ? <span className={styles.coverBadge}>Absentee</span> : null}
          pill={<span className={styles.pill} data-source="pipeline">Pipeline</span>}
          footer={
            (shownFlags.length > 0 || score !== null) ? (
              <>
                {shownFlags.length > 0 && (
                  <div className={styles.flagRow}>
                    {shownFlags.map((f) => (
                      <span key={f} className={styles.flagOnCover}>{flagLabel(f)}</span>
                    ))}
                    {extraFlags > 0 && <span className={styles.flagOnCover}>+{extraFlags}</span>}
                  </div>
                )}
                {score !== null && (
                  <div className={styles.meterRow} title={`Motivation ${score} / 100`}>
                    <span className={styles.meterLabel}>Motivation</span>
                    <span className={styles.meterTrack} aria-hidden="true">
                      <span
                        className={styles.meterFill}
                        data-tier={motivationTier(score)}
                        style={{ width: `${clampPct(score)}%` }}
                      />
                    </span>
                    <span className={styles.meterVal}>{score}</span>
                  </div>
                )}
              </>
            ) : null
          }
        />
        <div className={styles.body}>
          {price ? <span className={styles.price}>{price}</span> : <span className={styles.priceTbd}>Unpriced</span>}
          <span className={styles.address}>{l.address || 'Address pending'}</span>
          <span className={styles.locale}>
            {[l.city, l.state].filter(Boolean).join(', ')}{l.zip ? ` ${l.zip}` : ''}
          </span>
          {l.owner_name && <span className={styles.owner}>Owner · {l.owner_name}</span>}

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
}
