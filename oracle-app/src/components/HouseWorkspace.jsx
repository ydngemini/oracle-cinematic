import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Building2, ExternalLink, Link2, MapPin, ScanLine } from 'lucide-react';
import HouseLinkDialog from './HouseLinkDialog';
import { useTour } from '../state/useTour';
import { tourOffer } from '../lib/tour/tourOffer';
import styles from './HouseSelection.module.css';

const PropertyTour = lazy(() => import('./PropertyTour'));
// Tier-3 walkable interior. TourViewer picks the engine from VITE_TOUR_ENGINE
// (PlayCanvas by default since 2026-08-07, gsplat opt-in) so this surface stays
// engine-agnostic.
const TourViewer = lazy(() => import('./TourViewer'));

const PRICE_FORMAT = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});
const NUMBER_FORMAT = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });

function sourcedValue(value, formatter) {
  if (value === null || value === undefined || value === '') return 'Not published';
  return formatter ? formatter.format(Number(value)) : String(value);
}

function trapFocus(event, root) {
  if (event.key !== 'Tab') return;
  const focusable = [...(root?.querySelectorAll(
    'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  ) ?? [])];
  if (focusable.length === 0) {
    event.preventDefault();
    root?.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

export default function HouseWorkspace({ house, onHouseUpdate }) {
  const [linkOpen, setLinkOpen] = useState(false);
  const [tourOpen, setTourOpen] = useState(false);
  const [walkOpen, setWalkOpen] = useState(false);
  // Honest tier resolver — only yields splat_url when a real capture exists.
  const { tour } = useTour({ leadId: house?.lead_id ?? null });
  // Shared with PropertyViewTab. The wording is a claim about evidence, not
  // a caption, so it lives in one tested place rather than in two copies.
  const offer = tourOffer(tour);
  const [notice, setNotice] = useState('');
  const tourDialogRef = useRef(null);
  const tourButtonRef = useRef(null);

  useEffect(() => {
    if (!tourOpen) return undefined;
    const opener = tourButtonRef.current;
    window.requestAnimationFrame(() => tourDialogRef.current?.focus());
    return () => opener?.focus({ preventScroll: true });
  }, [tourOpen]);

  if (!house) {
    return (
      <aside className={styles.workspaceEmpty} aria-labelledby="workspace-empty-title">
        <Building2 aria-hidden="true" />
        <span>Selected house</span>
        <h2 id="workspace-empty-title">Choose a property record</h2>
        <p>The property workspace will open here with sourced facts, 3D access, and client linking.</p>
      </aside>
    );
  }

  const locality = [house.city, house.state, house.zip].filter(Boolean).join(', ');
  const hasAddress = Boolean(house.address);
  const factCoverage = house.fact_coverage;

  return (
    <aside className={styles.workspace} aria-labelledby="workspace-title">
      <div className={styles.workspaceTopline}>
        <span className={styles.sourceBadge} data-manual={house.isManual ? '' : undefined}>
          {house.isManual ? 'CRM manual · unverified' : 'Public record'}
        </span>
        {house.parcel_id && <span>Parcel {house.parcel_id}</span>}
      </div>

      <div className={styles.workspaceTitle}>
        <MapPin aria-hidden="true" />
        <div>
          <h2 id="workspace-title">{house.address || 'Address unavailable'}</h2>
          <p>{locality || house.county || 'Property workspace'}</p>
        </div>
      </div>

      <dl className={styles.facts}>
        <div>
          <dt>Assessor value</dt>
          <dd>{sourcedValue(house.price, PRICE_FORMAT)}</dd>
        </div>
        <div>
          <dt>Last recorded sale</dt>
          <dd>{sourcedValue(house.last_sale_price, PRICE_FORMAT)}</dd>
        </div>
        <div>
          <dt>Bedrooms</dt>
          <dd>{sourcedValue(house.beds, NUMBER_FORMAT)}</dd>
        </div>
        <div>
          <dt>Bathrooms</dt>
          <dd>{sourcedValue(house.baths, NUMBER_FORMAT)}</dd>
        </div>
        <div>
          <dt>Square feet</dt>
          <dd>{sourcedValue(house.sqft, NUMBER_FORMAT)}</dd>
        </div>
        <div>
          <dt>Lot square feet</dt>
          <dd>{sourcedValue(house.lot_sqft, NUMBER_FORMAT)}</dd>
        </div>
        <div>
          <dt>Year built</dt>
          <dd>{sourcedValue(house.year_built)}</dd>
        </div>
        <div>
          <dt>Total rooms</dt>
          <dd>{sourcedValue(house.rooms, NUMBER_FORMAT)}</dd>
        </div>
        <div>
          <dt>Property class</dt>
          <dd>{sourcedValue(house.property_class || house.property_type)}</dd>
        </div>
        <div>
          <dt>County</dt>
          <dd>{sourcedValue(house.county)}</dd>
        </div>
        <div>
          <dt>Owner of record</dt>
          <dd>{sourcedValue(house.owner_name)}</dd>
        </div>
        <div>
          <dt>Sale date</dt>
          <dd>
            {house.last_sale_date
              ? new Date(`${String(house.last_sale_date).slice(0, 10)}T00:00:00`).toLocaleDateString()
              : 'Not published'}
          </dd>
        </div>
      </dl>

      {factCoverage && (
        <p className={styles.factCoverage} role="status">
          {factCoverage.complete
            ? 'All 8 core property facts are source-backed.'
            : `${factCoverage.observed_count} of ${factCoverage.required_count} core facts published by this source. Missing facts are never estimated.`}
        </p>
      )}

      <div className={styles.provenance}>
        <ScanLine aria-hidden="true" />
        <div>
          <strong>{house.isManual ? 'Manual CRM entry' : 'Source-backed property data'}</strong>
          <span>
            {house.isManual
              ? 'No public facts are inferred. Link this address to a client to save it as a draft house.'
              : `${house.source || 'Public source'} · ${
                house.last_updated
                  ? `refreshed ${new Date(house.last_updated).toLocaleDateString()}`
                  : 'refresh date unavailable'
              }. Verify source facts before relying on them.`}
          </span>
        </div>
      </div>

      {notice && <div className={styles.successState} role="status">{notice}</div>}

      <div className={styles.workspaceActions}>
        <button
          ref={tourButtonRef}
          type="button"
          className={styles.secondaryButton}
          onClick={() => setTourOpen(true)}
          disabled={!hasAddress}
        >
          <ExternalLink aria-hidden="true" />
          Open 3D tour
        </button>
        {offer.kind === 'walkable' && (
          <button
            type="button"
            className={styles.secondaryButton}
            onClick={() => setWalkOpen(true)}
          >
            <Building2 aria-hidden="true" />
            {offer.label}
          </button>
        )}
        <button
          type="button"
          className={styles.primaryButton}
          onClick={() => setLinkOpen(true)}
          disabled={!hasAddress}
        >
          <Link2 aria-hidden="true" />
          Link to client
        </button>
      </div>

      {!hasAddress && (
        <p className={styles.addressWarning} role="status">
          This public record needs a usable address before it can open or link.
        </p>
      )}

      {linkOpen && (
        <HouseLinkDialog
          house={house}
          onClose={() => setLinkOpen(false)}
          onLinked={(client, result) => {
            setNotice(
              result?.created
                ? `Linked to ${client.full_name}.`
                : `Already linked to ${client.full_name}.`,
            );
            // The link created (or found) this tenant's lead for the parcel and
            // returned its id. Everything interior hangs off that id — the tour
            // resolver, the photo filmstrip, the tier badge — so hand it up
            // rather than discarding it and waiting for a refetch.
            const linked = result?.house;
            if (linked?.lead_id || linked?.listing_id) {
              onHouseUpdate?.({
                lead_id: linked.lead_id ?? house?.lead_id ?? null,
                listing_id: linked.listing_id ?? house?.listing_id ?? null,
              });
            }
            setLinkOpen(false);
          }}
        />
      )}

      {tourOpen && (
        <div
          ref={tourDialogRef}
          className={styles.tourLayer}
          role="dialog"
          aria-modal="true"
          aria-label={`3D tour for ${house.address}`}
          tabIndex={-1}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault();
              setTourOpen(false);
            } else {
              trapFocus(event, tourDialogRef.current);
            }
          }}
        >
          <Suspense fallback={<div className={styles.tourLoading} role="status">Opening property tour…</div>}>
            <PropertyTour
              address={house.address}
              lat={house.latitude == null ? undefined : Number(house.latitude)}
              lng={house.longitude == null ? undefined : Number(house.longitude)}
              leadId={house.lead_id || undefined}
              onClose={() => setTourOpen(false)}
              title={house.address}
            />
          </Suspense>
        </div>
      )}
      {walkOpen && offer.kind === 'walkable' && (
        <Suspense fallback={null}>
          <TourViewer
            splatUrl={tour.splat_url}
            panoScenes={tour.pano_scenes}
            tourpoints={tour.tourpoints}
            disclosure={tour.disclosure}
            floors={tour.floors}
            address={house.address}
            title={house.address}
            // The badge has to persist inside the viewer, not just on the button
            // that opened it — once someone is walking around, the entry point
            // is off screen and the space looks exactly like a real capture.
            isThisProperty={tour.is_this_property !== false}
            onClose={() => setWalkOpen(false)}
          />
        </Suspense>
      )}
    </aside>
  );
}
