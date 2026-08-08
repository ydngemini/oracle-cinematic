// google3d.js — lazy loader + geocoder for Google Maps Platform.
//
// Powers the photoreal 3D property tour via the Maps JavaScript API
// Map3DElement (<gmp-map-3d>) web component + Photorealistic 3D Tiles.
// Google's own renderer does the WebGL — no Three.js, no bundled 3D lib.
//
// NOTHING here runs at import time. The <script> tag is injected only when a
// caller invokes loadGoogleMaps() (i.e. when a tour is actually opened) AND a
// key is configured. With no key, callers render the graceful fallback instead.
//
// Every promise handed out here is bounded by a watchdog. A promise that never
// settles is worse than a rejection: the caller's loading veil never lifts, so
// the user watches a spinner forever with no way to learn why.

// The key is added to oracle-app/.env later as VITE_GOOGLE_MAPS_KEY. Until then
// this resolves to '' and hasMapsKey() is false everywhere.
const KEY = (import.meta.env.VITE_GOOGLE_MAPS_KEY || '').trim();
const MAP_ID = (import.meta.env.VITE_GOOGLE_MAP_ID || '').trim();

// Map3DElement (the maps3d library) ships on the alpha channel. Google moves it
// between channels without notice, and the channel is baked into the bootstrap
// URL (unswitchable once loaded), so keep it overridable without a code change.
const MAPS_VERSION = (import.meta.env.VITE_GOOGLE_MAPS_VERSION || '').trim() || 'alpha';

// Watchdogs — long enough for a cold cache on a slow link, short enough that a
// misconfigured key surfaces as an honest error instead of an endless spinner.
const BOOTSTRAP_TIMEOUT_MS = 15000; // <script> + the API's ready callback
const LIBRARY_TIMEOUT_MS = 15000;   // importLibrary('maps3d' | 'geocoding' | …)
const GEOCODE_TIMEOUT_MS = 12000;   // one geocode round-trip

export function getMapsKey() {
  return KEY;
}

export function hasMapsKey() {
  return KEY.length > 0;
}

// Advanced Markers require a map ID. DEMO_MAP_ID keeps local development
// usable, while production may provide a branded/cloud-configured map ID.
export function getMapsMapId() {
  return MAP_ID || 'DEMO_MAP_ID';
}

/** Reject with `code` if `promise` has not settled within `ms`. */
function withTimeout(promise, ms, code) {
  let timer = null;
  const watchdog = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(code)), ms);
  });
  return Promise.race([promise, authSignal(), watchdog]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

let _loadPromise = null;
let _script = null;
let _authFailed = false;
let _authSignal = null;
let _authReject = null;

/**
 * A promise that stays pending until Google reports an auth failure, then
 * rejects — forever. Raced against every bounded call below so an auth failure
 * fails FAST instead of idling out the watchdog: when the key is rejected the
 * bootstrap still loads fine, but the geocoder/tile requests simply never
 * settle, which otherwise costs a full 12–15 s of spinner per step.
 */
function authSignal() {
  if (!_authSignal) {
    _authSignal = new Promise((_, reject) => { _authReject = reject; });
    _authSignal.catch(() => { /* raced, never awaited alone */ });
  }
  return _authSignal;
}

function noteAuthFailure() {
  _authFailed = true;
  authSignal();
  _authReject?.(new Error('MAPS_AUTH_FAILED'));
}

/**
 * True once Google has reported an auth failure for this key: rejected key,
 * blocked HTTP referrer, Maps JavaScript API not enabled, or dead billing.
 * Callers use it to say WHY 3D is unavailable instead of blaming coverage.
 */
export function hadMapsAuthFailure() {
  return _authFailed;
}

/**
 * Re-arm after an auth failure so a retry gets a genuine second chance — the
 * fix (allowlisting a referrer, enabling an API) lands server-side and would
 * otherwise need a full page reload to be picked up.
 */
export function resetMapsAuthFailure() {
  _authFailed = false;
  _authSignal = null;
  _authReject = null;
}

/**
 * Inject the Maps JS API <script> exactly once and resolve with window.google.
 * Uses the async bootstrap so google.maps.importLibrary() is available for the
 * maps3d / geocoding libraries. Rejects with a coded Error when no key is set
 * or the script fails to load — callers map the code to a fallback reason.
 *
 * @returns {Promise<typeof window.google>}
 */
export function loadGoogleMaps() {
  if (!hasMapsKey()) {
    return Promise.reject(new Error('NO_KEY'));
  }
  if (_loadPromise) return _loadPromise;

  _loadPromise = new Promise((resolve, reject) => {
    // Already present (a prior mount, or HMR re-eval) — reuse it.
    if (window.google?.maps?.importLibrary) {
      resolve(window.google);
      return;
    }

    // Use the official `callback` param, NOT script.onload: under loading=async
    // onload fires before google.maps.importLibrary is initialised, so the old
    // code intermittently rejected MAPS_UNAVAILABLE ("3D map service could not be
    // reached"). The callback fires only once the API core is ready.
    const CB = '__neohGmapsReady';
    let timer = null;
    let settled = false;

    const finish = (fn) => {
      if (settled) return;
      settled = true;
      if (timer) { clearTimeout(timer); timer = null; }
      try { delete window[CB]; } catch { /* ignore */ }
      fn();
    };

    const fail = (code) => finish(() => {
      // Never keep a dead promise cached: every later mount would await the
      // same never-resolving promise and the tour would stay stuck (spinner
      // forever) for the rest of the page session, reopen or not.
      _loadPromise = null;
      // Drop the tag too, so a retry actually re-fetches the bootstrap.
      if (_script) {
        try { _script.remove(); } catch { /* already detached */ }
        _script = null;
      }
      reject(new Error(code));
    });

    // Google calls gm_authFailure for a rejected key, a blocked referrer, a
    // disabled API or dead billing. In every one of those cases the bootstrap
    // script still loads with HTTP 200 — so script.onerror never fires AND the
    // `callback` is never invoked. Without this handler the promise stays
    // pending forever, which is exactly what wedges the loading veil.
    //
    // Observed with RefererNotAllowedMapError: the bootstrap DOES complete and
    // the callback fires, and the rejection only shows up when the first
    // service call (geocode, tiles) silently never settles. `fail()` no-ops
    // once the load has settled, so noteAuthFailure() is what actually cuts
    // those later calls short — and tells the UI to blame the key.
    window.gm_authFailure = () => {
      noteAuthFailure();
      fail('MAPS_AUTH_FAILED');
    };

    window[CB] = () => {
      if (window.google?.maps?.importLibrary) {
        finish(() => resolve(window.google));
      } else {
        fail('MAPS_UNAVAILABLE');
      }
    };

    // Backstop for the failure modes Google reports through no channel at all
    // (blocked by an extension/CSP, offline mid-fetch, silent bootstrap stall).
    timer = setTimeout(() => fail('MAPS_TIMEOUT'), BOOTSTRAP_TIMEOUT_MS);

    const params = new URLSearchParams({
      key: KEY,
      v: MAPS_VERSION,
      loading: 'async',
      libraries: 'maps3d,geocoding,marker',
      callback: CB,
    });

    const script = document.createElement('script');
    script.src = `https://maps.googleapis.com/maps/api/js?${params.toString()}`;
    script.async = true;
    script.defer = true;
    script.dataset.oracleGoogleMaps = '1';
    script.onerror = () => fail('SCRIPT_LOAD_FAILED');
    _script = script;
    document.head.appendChild(script);
  });

  return _loadPromise;
}

/**
 * Load the Photorealistic 3D library.
 * @returns {Promise<{ Map3DElement: any, Map3DMode: any }>}
 */
export async function loadMaps3d() {
  const google = await loadGoogleMaps();
  return withTimeout(
    google.maps.importLibrary('maps3d'),
    LIBRARY_TIMEOUT_MS,
    'MAPS3D_TIMEOUT',
  );
}

/** Load the standard map and accessible Advanced Marker libraries on demand. */
export async function loadMapsAndMarkers() {
  const google = await loadGoogleMaps();
  const [{ Map }, { AdvancedMarkerElement }] = await withTimeout(
    Promise.all([
      google.maps.importLibrary('maps'),
      google.maps.importLibrary('marker'),
    ]),
    LIBRARY_TIMEOUT_MS,
    'MAPS_LIB_TIMEOUT',
  );
  return { google, Map, AdvancedMarkerElement };
}

/**
 * Geocode a free-form street address to coordinates, client-side, same key.
 * @param {string} address
 * @returns {Promise<{ lat: number, lng: number, formatted: string }>}
 * @throws Error('NO_ADDRESS' | 'NO_RESULTS' | 'GEOCODE_FAILED' | 'GEOCODE_TIMEOUT')
 */
export async function geocodeAddress(address) {
  const clean = (address || '').trim();
  if (!clean) throw new Error('NO_ADDRESS');

  const google = await loadGoogleMaps();
  const { Geocoder } = await withTimeout(
    google.maps.importLibrary('geocoding'),
    LIBRARY_TIMEOUT_MS,
    'GEOCODE_TIMEOUT',
  );
  const geocoder = new Geocoder();

  // geocode() rejects on ZERO_RESULTS / REQUEST_DENIED / OVER_QUERY_LIMIT with a
  // prose message — normalise to codes the caller already knows how to render.
  let results;
  try {
    ({ results } = await withTimeout(
      geocoder.geocode({ address: clean }),
      GEOCODE_TIMEOUT_MS,
      'GEOCODE_TIMEOUT',
    ));
  } catch (err) {
    if (err?.message === 'GEOCODE_TIMEOUT') throw err;
    if (String(err?.code || err?.message || '').includes('ZERO_RESULTS')) {
      throw new Error('NO_RESULTS', { cause: err });
    }
    throw new Error('GEOCODE_FAILED', { cause: err });
  }
  if (!results || results.length === 0) throw new Error('NO_RESULTS');

  const loc = results[0].geometry.location;
  // location exposes lat()/lng() as methods; tolerate plain numbers too.
  const lat = typeof loc.lat === 'function' ? loc.lat() : loc.lat;
  const lng = typeof loc.lng === 'function' ? loc.lng() : loc.lng;
  return { lat, lng, formatted: results[0].formatted_address || clean };
}
