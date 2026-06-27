import { useCallback, useEffect, useRef, useState } from 'react';
import { hasMapsKey, loadMaps3d, geocodeAddress } from '../lib/google3d';
import { crmGet } from '../state/useCrmApi';
import styles from './PropertyTour.module.css';

/**
 * PropertyTour — WALKABLE photoreal 3D tour of the ACTUAL house.
 *
 * Renders Google Photorealistic 3D Tiles through the Maps JavaScript API
 * Map3DElement (<gmp-map-3d>) web component. Google's renderer satisfies the
 * project's no-Three.js / no-bundled-3D rule.
 *
 * Two ways to experience it:
 *   • Auto-orbit  — a scripted camera sweep over the rooftop (Play/Pause).
 *   • Walk        — the element is interactive: drag to look, scroll/pinch to
 *                   move and tilt. The first grab pauses the orbit so the user
 *                   takes over; on-screen zoom/tilt buttons drive it on touch.
 *
 * Uploaded 2D photos (the agent's / homeowner's pictures) compose INTO the tour:
 * a filmstrip floats over the live 3D scene, a lightbox enlarges any shot, and —
 * where the maps3d alpha supports it — numbered photo-pins are anchored at the
 * house in 3D and open the matching photo on click. This is compositing the
 * real photos over Google's photoreal world, NOT photogrammetric reconstruction
 * (a mesh rebuild is a separate cloud step, out of scope here).
 *
 * Pipeline: address -> client-side geocode -> mount <gmp-map-3d> centred on the
 * house -> orbit + interaction. Falls back cleanly when no key is configured or
 * Google has no 3D coverage — never crashes, never shows a broken map.
 *
 * Props:
 *   address  {string}            street address of the property (required)
 *   lat,lng  {number}            optional pre-known coords (skips geocoding)
 *   leadId / listingId           optional — fetch + compose that record's photos
 *   onClose  {() => void}        optional — dismiss the tour / return to 2D
 *   title    {string}            optional header label (defaults to the address)
 */

// Scripted-orbit framing tuned for a single-family home.
const ORBIT_RANGE = 150; // metres from the house — reads the roofline + lot
const ORBIT_TILT = 65;   // degrees — cinematic low-angle flyover
const ORBIT_MS = 60000;  // ~60s per revolution — slow, smooth, non-nauseating
const MANUAL_DEG_PER_FRAME = 0.12; // fallback orbit speed (~7°/s @ 60fps)

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

function mediaSrc(m) {
  if (!m) return '';
  const u = m.url;
  if (u) return /^https?:\/\//i.test(u) ? u : `${API_BASE}${u.startsWith('/') ? '' : '/'}${u}`;
  return m.id != null ? `${API_BASE}/api/media/${m.id}` : '';
}

const FALLBACK_COPY = {
  'no-key': 'Photoreal 3D is not configured yet.',
  address: 'We could not locate this address for a 3D flyover.',
  coverage: 'Photoreal 3D is unavailable for this address.',
  load: 'The 3D map service could not be reached.',
  error: 'Photoreal 3D is unavailable for this address.',
};

export default function PropertyTour({ address, lat, lng, leadId, listingId, onClose, title }) {
  const hostRef = useRef(null);   // div that hosts the <gmp-map-3d> element
  const mapRef = useRef(null);    // the Map3DElement instance
  const centerRef = useRef(null); // resolved { lat, lng, altitude }
  const rafRef = useRef(null);    // manual-orbit rAF handle (fallback path only)
  const playingRef = useRef(true);
  const markersRef = useRef([]);  // photo-pin Marker3D elements (best-effort)

  // Key absent → go straight to the graceful fallback, never touch the network.
  const [status, setStatus] = useState(() => (hasMapsKey() ? 'loading' : 'unavailable'));
  const [reason, setReason] = useState(hasMapsKey() ? null : 'no-key');
  const [playing, setPlaying] = useState(true);
  const [resolvedLabel, setResolvedLabel] = useState('');
  const [hintVisible, setHintVisible] = useState(true);

  // Composed photos.
  const [media, setMedia] = useState([]);
  const [lightbox, setLightbox] = useState(null); // index into media, or null

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
          repeatCount: 1,   // alpha API renamed `rounds` -> `repeatCount`
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

  // The user grabbed the scene — hand them control: stop the orbit, drop the hint.
  const takeControl = useCallback(() => {
    setHintVisible(false);
    if (!playingRef.current) return;
    playingRef.current = false;
    setPlaying(false);
    stopOrbit();
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
    setHintVisible(true);
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
        const maps3d = await loadMaps3d();
        const { Map3DElement } = maps3d;
        // The alpha maps3d API renamed the mode enum `Map3DMode` -> `MapMode`
        // (the old name now resolves to undefined → the element never initialised
        // its mode → gmp-error → fallback). Prefer MapMode, fall back to the old
        // name so we work across either API revision.
        const MapMode = maps3d.MapMode ?? maps3d.Map3DMode;
        if (cancelled || !hostRef.current) return;

        const center = { lat: coords.lat, lng: coords.lng, altitude: 0 };
        centerRef.current = center;

        const map = new Map3DElement({
          center,
          range: ORBIT_RANGE,
          tilt: ORBIT_TILT,
          heading: 0,
          mode: MapMode?.HYBRID ?? MapMode?.SATELLITE,
        });
        map.style.width = '100%';
        map.style.height = '100%';
        map.style.display = 'block';

        // Make the scene WALKABLE. Map3DElement is interactive by default; where
        // the alpha exposes a gesture-handling knob, set it wide-open so drag-pan,
        // scroll-zoom and tilt all work. Guarded — interaction works regardless.
        const GH = maps3d.GestureHandling;
        try {
          map.gestureHandling = GH?.GREEDY ?? GH?.ALL ?? GH?.COOPERATIVE ?? 'greedy';
        } catch { /* interactive by default — ignore */ }

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
        // First user gesture yields control from the orbit to the walker.
        map.addEventListener('pointerdown', takeControl);
        map.addEventListener('wheel', takeControl, { passive: true });

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
      markersRef.current = [];
    };
  }, [address, lat, lng, title, startOrbit, stopOrbit, takeControl]);

  // ── Fetch the record's uploaded photos (compose into the tour) ────────────
  useEffect(() => {
    if (leadId == null && listingId == null) { setMedia([]); return; }
    let alive = true;
    const p = new URLSearchParams();
    if (leadId != null) p.set('lead_id', String(leadId));
    if (listingId != null) p.set('listing_id', String(listingId));
    crmGet(`/api/crm/media?${p.toString()}`).then(
      (data) => {
        if (!alive) return;
        const list = (Array.isArray(data?.media) ? data.media : [])
          .slice()
          .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
        setMedia(list);
      },
      () => { if (alive) setMedia([]); } // no photos overlay — degrade quietly
    );
    return () => { alive = false; };
  }, [leadId, listingId]);

  // ── Anchor numbered photo-pins in the 3D scene (best-effort stretch) ──────
  useEffect(() => {
    if (status !== 'ready' || media.length === 0) return;
    const map = mapRef.current;
    const c = centerRef.current;
    if (!map || !c) return;
    let cancelled = false;
    const created = [];

    (async () => {
      try {
        const maps3d = await loadMaps3d();
        const Marker = maps3d.Marker3DInteractiveElement || maps3d.Marker3DElement;
        if (!Marker || cancelled) return;
        const AltitudeMode = maps3d.AltitudeMode;
        const pins = media.slice(0, 8);
        pins.forEach((m, i) => {
          // Honesty rule: only drop a 3D pin where the photo carries REAL
          // coordinates. With no media geolocation we skip the pin entirely and
          // leave the filmstrip/lightbox as the way to browse photos — never
          // fabricate a spatial position from the photo index.
          const mediaLat = Number(m.lat ?? m.latitude);
          const mediaLng = Number(m.lng ?? m.longitude);
          if (!Number.isFinite(mediaLat) || !Number.isFinite(mediaLng)) return;
          const mediaAltitude = Number(m.altitude);
          const position = {
            lat: mediaLat,
            lng: mediaLng,
            altitude: Number.isFinite(mediaAltitude) ? mediaAltitude : 0,
          };
          let marker;
          try {
            marker = new Marker({
              position,
              altitudeMode: AltitudeMode?.RELATIVE_TO_GROUND ?? undefined,
              label: String(i + 1),
              extruded: true,
            });
          } catch {
            try { marker = new Marker({ position, label: String(i + 1) }); }
            catch { return; }
          }
          if (typeof marker.addEventListener === 'function') {
            marker.addEventListener('gmp-click', () => setLightbox(i));
          }
          try { map.append(marker); created.push(marker); } catch { /* unsupported */ }
        });
        markersRef.current = created;
      } catch { /* photo-pins are a best-effort enhancement — never fatal */ }
    })();

    return () => {
      cancelled = true;
      created.forEach((mk) => { try { mk.remove(); } catch { /* detached */ } });
      markersRef.current = [];
    };
  }, [status, media]);

  // Auto-dismiss the walk hint after a few seconds.
  useEffect(() => {
    if (status !== 'ready' || !hintVisible) return;
    const t = setTimeout(() => setHintVisible(false), 6000);
    return () => clearTimeout(t);
  }, [status, hintVisible]);

  // Lightbox keyboard nav.
  useEffect(() => {
    if (lightbox == null) return;
    const onKey = (e) => {
      if (e.key === 'Escape') setLightbox(null);
      else if (e.key === 'ArrowRight') setLightbox((i) => (i == null ? i : (i + 1) % media.length));
      else if (e.key === 'ArrowLeft') setLightbox((i) => (i == null ? i : (i - 1 + media.length) % media.length));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [lightbox, media.length]);

  // ── Controls ──────────────────────────────────────────────────────────────
  const togglePlay = useCallback(() => {
    const next = !playingRef.current;
    playingRef.current = next;
    setPlaying(next);
    if (next) { setHintVisible(false); startOrbit(); }
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

  // Manual camera nudges — they take control from the orbit first (touch-friendly).
  const nudge = useCallback((fn) => {
    const map = mapRef.current;
    if (!map) return;
    takeControl();
    try { fn(map); } catch { /* element detached */ }
  }, [takeControl]);

  const zoomIn = useCallback(() => nudge((m) => { m.range = Math.max(40, (m.range || ORBIT_RANGE) * 0.78); }), [nudge]);
  const zoomOut = useCallback(() => nudge((m) => { m.range = Math.min(2200, (m.range || ORBIT_RANGE) * 1.28); }), [nudge]);
  const tiltUp = useCallback(() => nudge((m) => { m.tilt = Math.min(85, (m.tilt ?? ORBIT_TILT) + 8); }), [nudge]);
  const tiltDown = useCallback(() => nudge((m) => { m.tilt = Math.max(0, (m.tilt ?? ORBIT_TILT) - 8); }), [nudge]);

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

          {/* Even with no 3D coverage, composed photos are still worth showing. */}
          {media.length > 0 && (
            <div className={styles.fallbackStrip}>
              {media.slice(0, 8).map((m, i) => (
                <button key={m.id ?? i} type="button" className={styles.fallbackThumb} onClick={() => setLightbox(i)} aria-label={`View photo ${i + 1}`}>
                  <img src={mediaSrc(m)} alt={`Property photo ${i + 1}`} loading="lazy" decoding="async" />
                </button>
              ))}
            </div>
          )}

          {onClose && (
            <button type="button" className={styles.fallbackBtn} onClick={onClose}>
              Back to standard view
            </button>
          )}
        </div>

        {lightbox != null && media[lightbox] && (
          <Lightbox media={media} index={lightbox} onClose={() => setLightbox(null)} onIndex={setLightbox} />
        )}
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

      {/* Walk hint — fades after the first grab or a few seconds. */}
      {status === 'ready' && hintVisible && (
        <div className={styles.hint} role="status">
          <span className={styles.hintGlyph} aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 11V5.5a1.5 1.5 0 0 1 3 0V11" />
              <path d="M12 11V4.5a1.5 1.5 0 0 1 3 0V11" />
              <path d="M15 11V6.5a1.5 1.5 0 0 1 3 0V14a6 6 0 0 1-6 6h-1.5a5 5 0 0 1-3.6-1.5l-3.2-3.4a1.6 1.6 0 0 1 2.3-2.2L9 14.5V7a1.5 1.5 0 0 1 3 0" />
            </svg>
          </span>
          Drag to look · scroll to move · pinch to tilt
        </div>
      )}

      {/* Dock — filmstrip of composed photos + walk controls, anchored bottom. */}
      {status === 'ready' && (
        <div className={styles.dock}>
          {media.length > 0 && (
            <div className={styles.filmstrip} aria-label="Property photos">
              {media.map((m, i) => (
                <button
                  key={m.id ?? i}
                  type="button"
                  className={styles.film}
                  onClick={() => setLightbox(i)}
                  aria-label={`View photo ${i + 1}`}
                >
                  <img src={mediaSrc(m)} alt={`Property photo ${i + 1}`} loading="lazy" decoding="async" />
                  <span className={styles.filmTag}>{i + 1}</span>
                </button>
              ))}
            </div>
          )}

          <div className={styles.controls}>
            <button type="button" className={styles.ctrlBtn} onClick={togglePlay} aria-pressed={playing}>
              <span className={styles.ctrlGlyph} aria-hidden="true">
                {playing ? (
                  <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1" /><rect x="14" y="5" width="4" height="14" rx="1" /></svg>
                ) : (
                  <svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5l12 7-12 7z" /></svg>
                )}
              </span>
              {playing ? 'Orbit' : 'Walk'}
            </button>

            <span className={styles.ctrlSep} aria-hidden="true" />

            <button type="button" className={styles.iconCtrl} onClick={zoomIn} aria-label="Zoom in">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.4-4.4M11 8.5v5M8.5 11h5" /></svg>
            </button>
            <button type="button" className={styles.iconCtrl} onClick={zoomOut} aria-label="Zoom out">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.4-4.4M8.5 11h5" /></svg>
            </button>
            <button type="button" className={styles.iconCtrl} onClick={tiltUp} aria-label="Tilt up">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M12 19V6M6 11l6-6 6 6" /></svg>
            </button>
            <button type="button" className={styles.iconCtrl} onClick={tiltDown} aria-label="Tilt down">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v13M6 13l6 6 6-6" /></svg>
            </button>

            <span className={styles.ctrlSep} aria-hidden="true" />

            <button type="button" className={styles.iconCtrl} onClick={snapToAddress} aria-label="Snap to house">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 21s-6.5-5.6-6.5-10A6.5 6.5 0 0 1 12 4.5 6.5 6.5 0 0 1 18.5 11c0 4.4-6.5 10-6.5 10Z" />
                <circle cx="12" cy="11" r="2.2" />
              </svg>
            </button>
          </div>
        </div>
      )}

      <span className={styles.attribution}>Imagery © Google</span>

      {lightbox != null && media[lightbox] && (
        <Lightbox media={media} index={lightbox} onClose={() => setLightbox(null)} onIndex={setLightbox} />
      )}
    </div>
  );
}

// ── Lightbox over the live 3D flyover ────────────────────────────────────────
function Lightbox({ media, index, onClose, onIndex }) {
  const count = media.length;
  const dialogRef = useRef(null);
  const closeRef = useRef(null);

  // Move focus into the dialog on open, restore it to the opener on close.
  useEffect(() => {
    const previouslyFocused = document.activeElement;
    closeRef.current?.focus();
    return () => previouslyFocused?.focus?.();
  }, []);

  // Keep Tab/Shift+Tab cycling within the dialog's controls.
  const trapFocus = useCallback((e) => {
    if (e.key !== 'Tab') return;
    const focusable = [...dialogRef.current.querySelectorAll('button')];
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last?.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first?.focus();
    }
  }, []);

  return (
    <div
      ref={dialogRef}
      className={styles.lightbox}
      role="dialog"
      aria-modal="true"
      aria-label={`Photo ${index + 1} of ${count}`}
      onKeyDown={trapFocus}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <img className={styles.lightboxImg} src={mediaSrc(media[index])} alt={`Property photo ${index + 1}`} />
      <button ref={closeRef} type="button" className={styles.lbClose} onClick={onClose} aria-label="Close photo">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
      </button>
      {count > 1 && (
        <>
          <button type="button" className={`${styles.lbNav} ${styles.lbPrev}`} onClick={() => onIndex((index - 1 + count) % count)} aria-label="Previous photo">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6" /></svg>
          </button>
          <button type="button" className={`${styles.lbNav} ${styles.lbNext}`} onClick={() => onIndex((index + 1) % count)} aria-label="Next photo">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M9 18l6-6-6-6" /></svg>
          </button>
          <span className={styles.lbCounter}>{index + 1} / {count}</span>
        </>
      )}
    </div>
  );
}
