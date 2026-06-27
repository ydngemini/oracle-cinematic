// google3d.js — lazy loader + geocoder for Google Maps Platform.
//
// Powers the photoreal 3D property tour via the Maps JavaScript API
// Map3DElement (<gmp-map-3d>) web component + Photorealistic 3D Tiles.
// Google's own renderer does the WebGL — no Three.js, no bundled 3D lib.
//
// NOTHING here runs at import time. The <script> tag is injected only when a
// caller invokes loadGoogleMaps() (i.e. when a tour is actually opened) AND a
// key is configured. With no key, callers render the graceful fallback instead.

// The key is added to oracle-app/.env later as VITE_GOOGLE_MAPS_KEY. Until then
// this resolves to '' and hasMapsKey() is false everywhere.
const KEY = (import.meta.env.VITE_GOOGLE_MAPS_KEY || '').trim();

// Map3DElement (the maps3d library) currently ships only on the alpha channel.
const MAPS_VERSION = 'alpha';

export function getMapsKey() {
  return KEY;
}

export function hasMapsKey() {
  return KEY.length > 0;
}

let _loadPromise = null;

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
    window[CB] = () => {
      try { delete window[CB]; } catch { /* ignore */ }
      if (window.google?.maps?.importLibrary) resolve(window.google);
      else reject(new Error('MAPS_UNAVAILABLE'));
    };

    const params = new URLSearchParams({
      key: KEY,
      v: MAPS_VERSION,
      loading: 'async',
      libraries: 'maps3d,geocoding',
      callback: CB,
    });

    const script = document.createElement('script');
    script.src = `https://maps.googleapis.com/maps/api/js?${params.toString()}`;
    script.async = true;
    script.defer = true;
    script.dataset.oracleGoogleMaps = '1';
    script.onerror = () => {
      _loadPromise = null; // allow a later retry after a transient failure
      reject(new Error('SCRIPT_LOAD_FAILED'));
    };
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
  return google.maps.importLibrary('maps3d');
}

/**
 * Geocode a free-form street address to coordinates, client-side, same key.
 * @param {string} address
 * @returns {Promise<{ lat: number, lng: number, formatted: string }>}
 * @throws Error('NO_ADDRESS' | 'NO_RESULTS') — caller renders the fallback.
 */
export async function geocodeAddress(address) {
  const clean = (address || '').trim();
  if (!clean) throw new Error('NO_ADDRESS');

  const google = await loadGoogleMaps();
  const { Geocoder } = await google.maps.importLibrary('geocoding');
  const geocoder = new Geocoder();

  const { results } = await geocoder.geocode({ address: clean });
  if (!results || results.length === 0) throw new Error('NO_RESULTS');

  const loc = results[0].geometry.location;
  // location exposes lat()/lng() as methods; tolerate plain numbers too.
  const lat = typeof loc.lat === 'function' ? loc.lat() : loc.lat;
  const lng = typeof loc.lng === 'function' ? loc.lng() : loc.lng;
  return { lat, lng, formatted: results[0].formatted_address || clean };
}
