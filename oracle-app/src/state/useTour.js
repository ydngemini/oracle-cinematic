import { useEffect, useState } from 'react';
import { crmGet } from './useCrmApi';

/**
 * useTour — resolve a property's best available tour tier from the backend
 * resolver (GET /api/crm/property-tour). Single source of truth shared by
 * PropertyTour, WalkableSplatViewer, and the listing surfaces so they all agree
 * on which tour to offer + how to label it honestly.
 *
 * Async-only setState (no synchronous set-state-in-effect). Silent-null degrade:
 * if the resolver is offline we behave as exterior-only and never over-promise.
 *
 * @returns {{ tour: object|null }} tour = { best_tier, badge, honest_note,
 *   walkable_interior, disclosure, tiers, splat_url, pano_manifest_url, photo_count }
 */
export function useTour({ leadId, listingId }) {
  const [tour, setTour] = useState(null);

  useEffect(() => {
    if (leadId == null && listingId == null) return undefined;
    let alive = true;
    const p = new URLSearchParams();
    if (leadId != null) p.set('lead_id', String(leadId));
    if (listingId != null) p.set('listing_id', String(listingId));
    crmGet(`/api/crm/property-tour?${p.toString()}`).then(
      (d) => { if (alive) setTour(d); },
      () => { if (alive) setTour(null); },
    );
    return () => { alive = false; };
  }, [leadId, listingId]);

  return { tour };
}
