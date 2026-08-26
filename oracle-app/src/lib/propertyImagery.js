// propertyImagery.js — real Google property imagery for Neoh's listing surfaces.
//
// Pure URL builders + a Street View coverage probe, plus a tiny React hook that
// resolves the best available cover for an address (Street View facade →
// satellite static fallback → nothing). `<img src>` hits Google's static
// endpoints directly — no CORS, no SDK, no bundle cost. The metadata probe is a
// FREE Street View request, so we only ever paint a pano when one truly exists.
//
// Hard rule: with no key, no location, or ZERO_RESULTS coverage we resolve to a
// graceful empty state so the caller renders the ghost icon — never a broken img.
//
// UNUSED ON PURPOSE — do not "fix" this by wiring it into ListingsInventory.
// That was tried on 2026-08-26 and reverted. A listing card states a missing
// photo rather than filling the slot, and the decision is pinned by
// ListingsInventory.test.jsx › "says a photo is missing instead of showing a
// stand-in image". Street View is a real photograph of the property, but on a
// LISTING it reads as a claim about how complete the listing is, and the
// absence of an agent's own photo is information the card is meant to show.
//
// This module is kept, not deleted, because the code is correct and the blocker
// is a product decision rather than a defect. A legitimate home is a surface
// where imagery is not a completeness claim — a public parcel record being
// browsed before it is anyone's listing. Label the source wherever it lands;
// Google's terms require attribution on static imagery.

import { useEffect, useMemo, useState } from 'react';

const KEY = (import.meta.env.VITE_GOOGLE_MAPS_KEY || '').trim();

const STREETVIEW_BASE = 'https://maps.googleapis.com/maps/api/streetview';
const STREETVIEW_META = 'https://maps.googleapis.com/maps/api/streetview/metadata';
const STATICMAP_BASE = 'https://maps.googleapis.com/maps/api/staticmap';

export function hasMapsKey() {
  return KEY.length > 0;
}

/**
 * Build a single geocodable location string for a listing.
 * Prefers precise coordinates when the row carries them (tolerates a few key
 * spellings); otherwise composes "street, city, ST zip" from the parts the
 * cards already read. Returns '' when nothing usable is present.
 *
 * @param {object} l — a listing/lead row
 * @returns {string}
 */
export function locationFor(l) {
  if (!l || typeof l !== 'object') return '';

  const lat = firstNum(l.latitude, l.lat);
  const lng = firstNum(l.longitude, l.lng, l.lon);
  if (lat !== null && lng !== null) return `${lat},${lng}`;

  const street = String(l.address || '').trim();
  const city = String(l.city || '').trim();
  const state = String(l.state || l.state_code || '').trim();
  const zip = String(l.zip || l.zip_code || '').trim();

  const region = [state, zip].filter(Boolean).join(' ');
  return [street, city, region].filter(Boolean).join(', ');
}

function firstNum(...vals) {
  for (const v of vals) {
    if (v === null || v === undefined || v === '') continue;
    const n = typeof v === 'string' ? Number(v) : v;
    if (Number.isFinite(n) && n !== 0) return n;
  }
  return null;
}

/**
 * Street View Static facade URL. `return_error_code` makes a missing pano 404
 * (so an <img> fires onError and we fall through to satellite); `source=outdoor`
 * avoids indoor/business panos. Returns null when unusable.
 */
export function streetViewUrl(location, { size = '640x400', fov = 80, scale } = {}) {
  if (!KEY || !location) return null;
  const p = new URLSearchParams({
    size,
    location,
    fov: String(fov),
    source: 'outdoor',
    return_error_code: 'true',
    key: KEY,
  });
  if (scale) p.set('scale', String(scale));
  return `${STREETVIEW_BASE}?${p.toString()}`;
}

/**
 * Maps Static satellite/aerial URL — always renders a real tile for any
 * geocodable location, so it's the dependable fallback when no pano exists.
 */
export function satelliteUrl(location, { size = '640x400', zoom = 19, scale } = {}) {
  if (!KEY || !location) return null;
  const p = new URLSearchParams({
    center: location,
    zoom: String(zoom),
    size,
    maptype: 'satellite',
    key: KEY,
  });
  if (scale) p.set('scale', String(scale));
  return `${STATICMAP_BASE}?${p.toString()}`;
}

// Module-level dedup: many cards (and remounts) ask about the same address, so
// we share one in-flight probe per normalized location and remember definitive
// answers. Transient ERRORs are never cached, so a flaky network can recover.
const _coverageCache = new Map(); // normLocation -> 'OK' | 'ZERO_RESULTS'
const _coverageInflight = new Map(); // normLocation -> Promise<status>

function _normLocation(location) {
  return String(location).trim().toLowerCase();
}

async function _probeStreetViewCoverage(location) {
  try {
    const p = new URLSearchParams({ location, key: KEY });
    const res = await fetch(`${STREETVIEW_META}?${p.toString()}`);
    if (!res.ok) return 'ERROR';
    const data = await res.json();
    return data && typeof data.status === 'string' ? data.status : 'ERROR';
  } catch {
    // Network / transient — caller treats this as "optimistically try the pano".
    return 'ERROR';
  }
}

/**
 * Probe Street View coverage at a location (a FREE metadata request).
 * Deduped across callers: identical locations reuse a cached/in-flight result
 * instead of issuing a fresh network probe per card.
 * @returns {Promise<'OK'|'ZERO_RESULTS'|'NO_KEY'|'ERROR'>}
 */
export async function checkStreetViewCoverage(location) {
  if (!KEY) return 'NO_KEY';
  if (!location) return 'ERROR';

  const norm = _normLocation(location);
  if (_coverageCache.has(norm)) return _coverageCache.get(norm);
  if (_coverageInflight.has(norm)) return _coverageInflight.get(norm);

  const probe = _probeStreetViewCoverage(location)
    .then((status) => {
      // Only memoize definitive answers; let transient ERRORs retry next time.
      if (status !== 'ERROR') _coverageCache.set(norm, status);
      _coverageInflight.delete(norm);
      return status;
    })
    .catch(() => {
      _coverageInflight.delete(norm);
      return 'ERROR';
    });

  _coverageInflight.set(norm, probe);
  return probe;
}

/**
 * usePropertyCover — resolve an ordered list of real cover candidates for a
 * location, gated on a (free) coverage probe.
 *
 *   • no key / no location  → []          (caller shows the ghost)
 *   • coverage OK           → [streetview, satellite]
 *   • coverage ZERO_RESULTS → [satellite]  (no pano exists, skip it)
 *   • probe ERROR           → [streetview, satellite]  (optimistic; onError falls back)
 *
 * Returns { ready, candidates } — `ready` is false until the probe resolves so
 * the caller can hold a shimmer (no layout shift, no flash of the wrong image).
 *
 * @param {string} location
 * @param {{ size?: string, fov?: number, zoom?: number, scale?: number }} [opts]
 */
export function usePropertyCover(location, opts = {}) {
  const { size = '640x400', fov = 80, zoom = 19, scale } = opts;
  const active = hasMapsKey() && !!location;

  const [coverage, setCoverage] = useState('pending'); // pending | OK | ZERO_RESULTS | ERROR
  const [probedFor, setProbedFor] = useState(null);

  // Render-phase reset: when the target changes, snap back to 'pending' before
  // the probe re-runs. React-recommended over a setState-in-effect (no cascade).
  if (active && probedFor !== location) {
    setProbedFor(location);
    setCoverage('pending');
  }

  useEffect(() => {
    if (!active) return undefined;
    let alive = true;
    checkStreetViewCoverage(location).then((status) => {
      if (alive) setCoverage(status);
    });
    return () => {
      alive = false;
    };
  }, [active, location]);

  const candidates = useMemo(() => {
    if (!active || coverage === 'pending') return [];
    const sv = streetViewUrl(location, { size, fov, scale });
    const sat = satelliteUrl(location, { size, zoom, scale });
    const list = [];
    if (coverage !== 'ZERO_RESULTS' && sv) list.push({ url: sv, kind: 'streetview' });
    if (sat) list.push({ url: sat, kind: 'satellite' });
    return list;
  }, [active, coverage, location, size, fov, zoom, scale]);

  // When there's nothing to probe we're immediately "ready" with no candidates.
  return { ready: !active || coverage !== 'pending', candidates };
}
