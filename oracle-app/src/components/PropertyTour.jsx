import { useCallback, useEffect, useRef, useState } from 'react';
import { hasMapsKey, loadMaps3d, geocodeAddress } from '../lib/google3d';
import styles from './PropertyTour.module.css';

/**
 * PropertyTour — photoreal 3D flyover of the ACTUAL house.
 *
 * Renders Google Photorealistic 3D Tiles through the Maps JavaScript API
 * Map3DElement (<gmp-map-3d>) web component. Google's renderer satisfies the
 * project's no-Three.js / no-bundled-3D rule. A scripted camera orbit (heading
 * sweep at a low tilt over the rooftop) IS the walkthrough tour.
 *
 * Pipeline: address -> client-side geocode -> mount <gmp-map-3d> centred on the
 * house -> smooth orbit. Falls back cleanly when no key is configured or Google
 * has no 3D coverage for the address — never crashes, never shows a broken map.
 *
 * Props:
 *   address  {string}            street address of the property (required)
 *   lat,lng  {number}            optional pre-known coords (skips geocoding)
 *   onClose  {() => void}        optional — dismiss the tour / return to 2D
 *   title    {string}            optional header label (defaults to the address)
 */

// Scripted-orbit framing tuned for a single-family home.
const ORBIT_RANGE = 150; // metres from the house — reads the roofline + lot
const ORBIT_TILT = 65;   // degrees — cinematic low-angle flyover
const ORBIT_MS = 60000;  // ~60s per revolution — slow, smooth, non-nauseating
const MANUAL_DEG_PER_FRAME = 0.12; // fallback orbit speed (~7°/s @ 60fps)

const FALLBACK_COPY = {
  'no-key': 'Photoreal 3D is not configured yet.',
  address: 'We could not locate this address for a 3D flyover.',
  coverage: 'Photoreal 3D is unavailable for this address.',
  load: 'The 3D map service could not be reached.',
  error: 'Photoreal 3D is unavailable for this address.',
};

export default function PropertyTour({ address, lat, lng, onClose, title }) {
  const hostRef = useRef(null);   // div that hosts the <gmp-map-3d> element
  const mapRef = useRef(null);    // the Map3DElement instance
  const centerRef = useRef(null); // resolved { lat, lng, altitude }
  const rafRef = useRef(null);    // manual-orbit rAF handle (fallback path only)
  const playingRef = useRef(true);

  // Key absent → go straight to the graceful fallback, never touch the network.
  const [status, setStatus] = useState(() => (hasMapsKey() ? 'loading' : 'unavailable'));
  const [reason, setReason] = useState(hasMapsKey() ? null : 'no-key');
  const [playing, setPlaying] = useState(true);
  const [resolvedLabel, setResolvedLabel] = useState('');

  // ── Orbit control (refs so handlers stay stable across renders) ───────────
  const stopOrbit = useCallback(() => {
    const map = mapRef.current;
    if (map?.stopCameraAnimation) {
      try { map.stopCameraAnimation(); } catch { /* not animating */ }
    }
    if (rafRef.current) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  }, []);

  const startOrbit = useCallback(() => {
    const map = mapRef.current;
    const c = centerRef.current;
    if (!map || !c) return;
    stopOrbit();
    const heading = typeof map.heading === 'number' ? map.heading : 0;
    // Prefer Google's built-in scripted orbit; fall back to a manual rAF sweep.
    if (typeof map.flyCameraAround === 'function') {
      try {
        map.flyCameraAround({
          camera: { center: c, range: ORBIT_RANGE, tilt: ORBIT_TILT, heading },
          durationMillis: ORBIT_MS,
          rounds: 1,
        });
        return;
      } catch { /* fall through to manual */ }
    }
    // Manual heading sweep — function declaration is hoisted, so the recursive
    // requestAnimationFrame self-reference is clean (no use-before-declare).
    function frame() {
      const m = mapRef.current;
      if (!m || !centerRef.current || !playingRef.current) {
        rafRef.current = null;
        return;
      }
      const next = (((m.heading || 0) + MANUAL_DEG_PER_FRAME) % 360 + 360) % 360;
      try { m.heading = next; } catch { /* element detached */ }
      rafRef.current = requestAnimationFrame(frame);
    }
    rafRef.current = requestAnimationFrame(frame);
  }, [stopOrbit]);

  // ── Build the 3D scene once coords + the maps3d library resolve ───────────
  useEffect(() => {
    if (!hasMapsKey()) return;
    let cancelled = false;

    // Reset to the loading veil whenever the target address/coords change. This
    // is an intentional sync with an external system (the Google 3D element),
    // not derived state — same accepted pattern as PropertyCanvas's init flags.
    /* eslint-disable react-hooks/set-state-in-effect */
    setStatus('loading');
    setReason(null);
    setPlaying(true);
    /* eslint-enable react-hooks/set-state-in-effect */
    playingRef.current = true;

    (async () => {
      try {
        // 1. Resolve coordinates — use supplied lat/lng, else geocode.
        let coords;
        let label = title || address || '';
        if (Number.isFinite(lat) && Number.isFinite(lng)) {
          coords = { lat, lng };
        } else {
          const g = await geocodeAddress(address);
          coords = { lat: g.lat, lng: g.lng };
          label = title || g.formatted;
        }
        if (cancelled) return;

        // 2. Load the Photorealistic 3D library + mount the element.
        const { Map3DElement, Map3DMode } = await loadMaps3d();
        if (cancelled || !hostRef.current) return;

        const center = { lat: coords.lat, lng: coords.lng, altitude: 0 };
        centerRef.current = center;

        const map = new Map3DElement({
          center,
          range: ORBIT_RANGE,
          tilt: ORBIT_TILT,
          heading: 0,
          mode: Map3DMode?.HYBRID ?? Map3DMode?.SATELLITE,
        });
        map.style.width = '100%';
        map.style.height = '100%';
        map.style.display = 'block';

        // Tile load / coverage failures surface as element errors — degrade.
        const onElementError = () => {
          if (cancelled) return;
          setReason('coverage');
          setStatus('unavailable');
        };
        map.addEventListener('gmp-error', onElementError);
        map.addEventListener('error', onElementError);
        // Loop the orbit: when one revolution ends, start the next if playing.
        map.addEventListener('gmp-animationend', () => {
          if (!cancelled && playingRef.current) startOrbit();
        });

        hostRef.current.replaceChildren(map);
        mapRef.current = map;

        setResolvedLabel(label);
        setStatus('ready');
        startOrbit();
      } catch (err) {
        if (cancelled) return;
        const code = err?.message || '';
        const mapped =
          code === 'NO_KEY' ? 'no-key'
            : code === 'NO_ADDRESS' || code === 'NO_RESULTS' ? 'address'
              : code === 'SCRIPT_LOAD_FAILED' || code === 'MAPS_UNAVAILABLE' ? 'load'
                : 'error';
        setReason(mapped);
        setStatus('unavailable');
      }
    })();

    return () => {
      cancelled = true;
      stopOrbit();
      const map = mapRef.current;
      if (map) {
        try { map.remove(); } catch { /* already detached */ }
      }
      mapRef.current = null;
      centerRef.current = null;
    };
  }, [address, lat, lng, title, startOrbit, stopOrbit]);

  // ── Controls ──────────────────────────────────────────────────────────────
  const togglePlay = useCallback(() => {
    const next = !playingRef.current;
    playingRef.current = next;
    setPlaying(next);
    if (next) startOrbit();
    else stopOrbit();
  }, [startOrbit, stopOrbit]);

  const snapToAddress = useCallback(() => {
    const map = mapRef.current;
    const c = centerRef.current;
    if (!map || !c) return;
    stopOrbit();
    // Instant re-frame to the canonical house shot.
    try {
      map.center = c;
      map.range = ORBIT_RANGE;
      map.tilt = ORBIT_TILT;
      map.heading = 0;
    } catch { /* element detached */ }
    if (playingRef.current) startOrbit();
  }, [startOrbit, stopOrbit]);

  // ── Render ────────────────────────────────────────────────────────────────
  if (status === 'unavailable') {
    return (
      <div className={styles.tour} data-state="unavailable">
        <div className={styles.fallback} role="status">
          <span className={styles.fallbackGlyph} aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 10.5 12 3l9 7.5" />
              <path d="M5.5 9.5V20h13V9.5" />
              <path d="m4 4 16 16" />
            </svg>
          </span>
          <span className={styles.fallbackTitle}>Photoreal 3D unavailable</span>
          <span className={styles.fallbackHint}>
            {FALLBACK_COPY[reason] || FALLBACK_COPY.error}
            {reason === 'no-key' ? '' : ' Showing the standard view instead.'}
          </span>
          {address && <span className={styles.fallbackAddr}>{address}</span>}
          {onClose && (
            <button type="button" className={styles.fallbackBtn} onClick={onClose}>
              Back to standard view
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className={styles.tour} data-state={status}>
      {/* Google renders the photoreal tiles + its own required logo/attribution
          into this host. We never hide that logo. */}
      <div ref={hostRef} className={styles.stage} />

      {status === 'loading' && (
        <div className={styles.loading} aria-live="polite">
          <span className={styles.spinner} aria-hidden="true" />
          <span className={styles.loadingText}>Building 3D flyover…</span>
        </div>
      )}

      <header className={styles.bar}>
        <span className={styles.kicker}>Neoh · 3D Tour</span>
        <span className={styles.addr} title={resolvedLabel || address}>
          {resolvedLabel || address}
        </span>
        {onClose && (
          <button type="button" className={styles.closeBtn} onClick={onClose} aria-label="Close 3D tour">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
              <path d="M6 6l12 12M18 6 6 18" />
            </svg>
          </button>
        )}
      </header>

      {status === 'ready' && (
        <div className={styles.controls}>
          <button
            type="button"
            className={styles.ctrlBtn}
            onClick={togglePlay}
            aria-pressed={playing}
          >
            <span className={styles.ctrlGlyph} aria-hidden="true">
              {playing ? (
                <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1" /><rect x="14" y="5" width="4" height="14" rx="1" /></svg>
              ) : (
                <svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5l12 7-12 7z" /></svg>
              )}
            </span>
            {playing ? 'Pause' : 'Play'}
          </button>
          <button type="button" className={styles.ctrlBtn} onClick={snapToAddress}>
            <span className={styles.ctrlGlyph} aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 21s-6.5-5.6-6.5-10A6.5 6.5 0 0 1 12 4.5 6.5 6.5 0 0 1 18.5 11c0 4.4-6.5 10-6.5 10Z" />
                <circle cx="12" cy="11" r="2.2" />
              </svg>
            </span>
            Snap to house
          </button>
        </div>
      )}

      <span className={styles.attribution}>Imagery © Google</span>
    </div>
  );
}
