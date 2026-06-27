import { lazy, Suspense, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import { locationFor, satelliteUrl, usePropertyCover } from '../lib/propertyImagery';
import MediaUploader from './MediaUploader';
import styles from './ListingDetail.module.css';

// The photoreal 3D tour pulls in the Google Maps loader on demand — keep it out
// of the initial chunk and only mount it when a tour is actually opened.
const PropertyTour = lazy(() => import('./PropertyTour'));

const GLYPHS = {
  house: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20h13V9.5" />
      <path d="M9.5 20v-6h5v6" />
    </svg>
  ),
  back: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M15 18l-6-6 6-6" />
    </svg>
  ),
  tour: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20h13V9.5" />
      <circle cx="12" cy="13.5" r="2" />
    </svg>
  ),
};

const fmtInt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

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

function formatDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
}

function normalizeStatus(s) {
  const t = String(s || '').toLowerCase();
  if (t.startsWith('active')) return 'active';
  if (t.startsWith('pend')) return 'pending';
  if (t.startsWith('sold') || t.startsWith('clos')) return 'sold';
  return 'other';
}

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

function flagLabel(f) {
  if (FLAG_LABELS[f]) return FLAG_LABELS[f];
  return String(f || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * ListingDetail — full record for one listing.
 * For MLS rows fetches GET /api/mls/listings/{id}; for harvested pipeline rows
 * fetches GET /api/mls/pipeline/{id}. Renders only real fields (a spec is
 * omitted entirely when the source didn't carry it — never a fabricated value).
 */
export default function ListingDetail({ listingId, source = 'mls', onBack }) {
  const [listing, setListing] = useState(null);
  const [error, setError] = useState(null);
  const [tourOpen, setTourOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    setListing(null);
    setError(null);
    const path = source === 'pipeline'
      ? `/api/mls/pipeline/${listingId}`
      : `/api/mls/listings/${listingId}`;
    crmGet(path).then(
      (data) => { if (alive) setListing(data); },
      (err) => { if (alive) setError(err); }
    );
    return () => { alive = false; };
  }, [listingId, source]);

  const l = listing;
  const isPipeline = (l?.source === 'pipeline') || source === 'pipeline';
  const status = normalizeStatus(l?.status);
  const price = formatPrice(l?.price);
  const photos = Array.isArray(l?.photos) ? l.photos : [];
  // Pipeline detail IDs are lead ids; MLS detail IDs are listing ids. The media
  // owner + the tour's photo source key off whichever this is.
  const mediaOwner = source === 'pipeline' ? { leadId: listingId } : { listingId };
  const mediaToken = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem('oracle_token') : null;
  const flags = Array.isArray(l?.distress_flags) ? l.distress_flags : [];

  // Geocodable location for Google imagery (prefers coords) + a textual address
  // for the 3D tour's geocoder.
  const coverLocation = locationFor(l);
  const textAddress = !l
    ? ''
    : [l.address, l.city, [l.state || l.state_code, l.zip || l.zip_code].filter(Boolean).join(' ')]
        .filter(Boolean)
        .join(', ');
  const tourLat = toNum(l?.latitude ?? l?.lat);
  const tourLng = toNum(l?.longitude ?? l?.lng);

  // Specs are rendered only when present — no zero-fills, no invented data.
  const specs = !l
    ? []
    : (isPipeline
        ? [
            ['Beds', toNum(l.beds) !== null ? fmtInt.format(toNum(l.beds)) : null],
            ['Baths', formatBaths(toNum(l.baths))],
            ['Sqft', toNum(l.sqft) ? fmtInt.format(toNum(l.sqft)) : null],
            ['Owner', l.owner_name || null],
            ['Owner type', l.owner_type ? flagLabel(l.owner_type) : null],
            ['Motivation', toNum(l.motivation_score) !== null ? `${fmtInt.format(toNum(l.motivation_score))} / 100` : null],
            ['Equity', toNum(l.equity_percent) !== null ? `${fmtInt.format(toNum(l.equity_percent) * 100)}%` : null],
            ['Last sale', formatDate(l.last_sale_date)],
            ['Parcel #', l.parcel_id || null],
            ['Stage', l.status || null],
          ].filter(([, v]) => v !== null && v !== '' && v !== undefined)
        : [
            ['Beds', toNum(l.beds) !== null ? fmtInt.format(toNum(l.beds)) : null],
            ['Baths', formatBaths(toNum(l.baths))],
            ['Sqft', toNum(l.sqft) ? fmtInt.format(toNum(l.sqft)) : null],
            ['Lot sqft', toNum(l.lot_sqft) ? fmtInt.format(toNum(l.lot_sqft)) : null],
            ['Year built', l.year_built || null],
            ['Property type', l.property_type || null],
            ['HOA / mo', toNum(l.hoa_monthly) ? `$${fmtInt.format(toNum(l.hoa_monthly))}` : null],
            ['Days on market', toNum(l.days_on_market) !== null ? fmtInt.format(toNum(l.days_on_market)) : null],
            ['County', l.county || null],
            ['MLS #', l.mls_number || null],
            ['Listed', formatDate(l.list_date)],
          ].filter(([, v]) => v !== null && v !== '' && v !== undefined));

  return (
    <section className={styles.wrap} aria-label="Listing detail">
      <button type="button" className={styles.backBtn} onClick={onBack}>
        {GLYPHS.back}
        <span>Back to results</span>
      </button>

      {error ? (
        <div className={styles.stateBox} role="alert">
          <span className={styles.errorTick} aria-hidden="true" />
          <p className={styles.stateText}>
            {error.status === 404 ? 'This listing is no longer available.' : (error.message || 'Couldn’t load this listing.')}
          </p>
        </div>
      ) : !l ? (
        <div className={styles.hero} aria-hidden="true">
          <div className={styles.skel} />
        </div>
      ) : (
        <>
          <ListingHero
            location={coverLocation}
            address={l.address || textAddress}
            pill={
              isPipeline ? (
                <span className={styles.pill} data-source="pipeline">Pipeline</span>
              ) : (
                <span className={styles.pill} data-status={status}>
                  {status === 'other' ? (l.status || '—') : status}
                </span>
              )
            }
            badge={isPipeline && l.is_absentee ? <span className={styles.coverBadge}>Absentee</span> : null}
          />

          {photos.length > 0 && (
            <div className={styles.thumbs}>
              {photos.slice(0, 8).map((src, i) => (
                <img
                  key={i}
                  className={styles.thumb}
                  src={src}
                  alt={l.address ? `Photo of ${l.address}` : 'Listing photo'}
                  loading="lazy"
                  decoding="async"
                  onError={(e) => { e.currentTarget.hidden = true; }}
                />
              ))}
            </div>
          )}

          <header className={styles.headBlock}>
            {price ? <span className={styles.price}>{price}</span> : <span className={styles.priceTbd}>{isPipeline ? 'Unpriced' : 'Price TBD'}</span>}
            <h2 className={styles.address}>{l.address || 'Address pending'}</h2>
            <p className={styles.locale}>
              {[l.city, l.state].filter(Boolean).join(', ')}{l.zip ? ` ${l.zip}` : ''}
            </p>
          </header>

          {specs.length > 0 && (
            <dl className={styles.specGrid}>
              {specs.map(([label, value]) => (
                <div key={label} className={styles.spec}>
                  <dt className={styles.specLabel}>{label}</dt>
                  <dd className={styles.specValue}>{value}</dd>
                </div>
              ))}
            </dl>
          )}

          {isPipeline && flags.length > 0 && (
            <div className={styles.flagBlock}>
              <h3 className={styles.descHead}>Distress signals</h3>
              <div className={styles.flagRow}>
                {flags.map((f) => (
                  <span key={f} className={styles.flag}>{flagLabel(f)}</span>
                ))}
              </div>
            </div>
          )}

          {l.description && (
            <div className={styles.descBlock}>
              <h3 className={styles.descHead}>About this home</h3>
              <p className={styles.descText}>{l.description}</p>
            </div>
          )}

          <MediaUploader {...mediaOwner} token={mediaToken} title="Property photos" />

          {textAddress && (
            <button
              type="button"
              className={styles.tourLaunch}
              onClick={() => setTourOpen(true)}
            >
              <span className={styles.tourLaunchGlyph} aria-hidden="true">{GLYPHS.tour}</span>
              Preview the 3D tour
            </button>
          )}

          <p className={styles.sourceNote}>
            {isPipeline
              ? `Real harvested pipeline lead${l.parcel_id ? ` · parcel ${l.parcel_id}` : ''}`
              : `Data via ${l.source === 'rentcast' ? 'RentCast' : (l.source || 'MLS')}, cached`}
            {formatDate(l.last_updated) ? ` · updated ${formatDate(l.last_updated)}` : ''}
          </p>

          {/* Photoreal 3D flyover — full-screen overlay; PropertyTour degrades
              cleanly to its own fallback when no Maps key is configured. */}
          {tourOpen && (
            <div
              className={styles.tourOverlay}
              role="dialog"
              aria-modal="true"
              aria-label="3D property tour"
              onClick={(e) => { if (e.target === e.currentTarget) setTourOpen(false); }}
            >
              <div className={styles.tourFrame}>
                <Suspense fallback={null}>
                  <PropertyTour
                    address={textAddress}
                    lat={tourLat ?? undefined}
                    lng={tourLng ?? undefined}
                    title={l.address || textAddress}
                    {...mediaOwner}
                    onClose={() => setTourOpen(false)}
                  />
                </Suspense>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}

// ── Street View hero (satellite static fallback → ghost) ─────────────────────

/**
 * ListingHero — big Street View facade for the listing, with a satellite static
 * minimap inset when a real pano is showing. Skeleton shimmer holds the 16/9 box
 * (no layout shift); an onError chain walks candidates before degrading to the
 * ghost glyph. `scale=2` keeps the hero crisp on retina displays.
 */
function ListingHero({ location, address, pill, badge }) {
  const { ready, candidates } = usePropertyCover(location, {
    size: '640x360',
    fov: 85,
    zoom: 19,
    scale: 2,
  });
  const [idx, setIdx] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [shownFor, setShownFor] = useState(location);

  // Render-phase reset when the target changes (no cascading effect).
  if (shownFor !== location) {
    setShownFor(location);
    setIdx(0);
    setLoaded(false);
  }

  const current = candidates[idx];
  const exhausted = ready && (candidates.length === 0 || idx >= candidates.length);
  const showShimmer = !exhausted && (!ready || !loaded);
  // Only inset the aerial when the hero itself is a Street View pano — otherwise
  // the hero is already the satellite tile and a minimap would be redundant.
  const minimap =
    current?.kind === 'streetview'
      ? satelliteUrl(location, { size: '320x320', zoom: 18, scale: 2 })
      : null;

  return (
    <div className={styles.hero}>
      <span className={styles.heroGhost} aria-hidden="true">{GLYPHS.house}</span>

      {current && (
        <img
          key={current.url}
          className={styles.heroImg}
          data-loaded={loaded ? 'true' : 'false'}
          src={current.url}
          alt={
            current.kind === 'satellite'
              ? (address ? `Satellite view of ${address}` : 'Satellite view of property')
              : (address ? `Street view of ${address}` : 'Property exterior')
          }
          decoding="async"
          onLoad={() => setLoaded(true)}
          onError={() => {
            setLoaded(false);
            setIdx((n) => n + 1);
          }}
        />
      )}

      {showShimmer && <span className={styles.skel} aria-hidden="true" />}
      {current && loaded && <span className={styles.heroScrim} aria-hidden="true" />}

      {pill}
      {badge}

      {minimap && loaded && (
        <span className={styles.heroMinimap}>
          <img
            className={styles.heroMinimapImg}
            src={minimap}
            alt=""
            loading="lazy"
            decoding="async"
            onError={(e) => { e.currentTarget.parentElement.hidden = true; }}
          />
          <span className={styles.heroMinimapTag}>Aerial</span>
        </span>
      )}

      {current && loaded && (
        <span className={styles.heroSourceTag}>
          {current.kind === 'streetview' ? 'Street View' : 'Satellite'}
        </span>
      )}
    </div>
  );
}
