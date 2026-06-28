import { useEffect, useRef, useState } from 'react';
import * as SPLAT from 'gsplat';
import styles from './WalkableSplatViewer.module.css';

/**
 * WalkableSplatViewer — first-person walk-INSIDE a property's Gaussian splat.
 *
 * Per-listing modal overlay (mirrors how PropertyTour is launched). Owns its own
 * gsplat renderer/scene/camera (never the global PropertyCanvas singleton) and a
 * compact first-person controller: WASD + drag-look on desktop, an on-screen
 * joystick + drag-look on touch. Movement is clamped to the loaded splat's bounds
 * (expanded) so you can't walk out into the void where the capture has no data.
 *
 * Tier-3 only — the resolver only hands us a splat_url for a real captured home.
 * The AI-reconstruction disclosure is shown persistently.
 *
 * Props: { splatUrl, disclosure?, address?, title?, onClose }
 */

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';
const absUrl = (u) => (!u ? '' : /^https?:\/\//i.test(u) ? u : `${API_BASE}${u.startsWith('/') ? '' : '/'}${u}`);
const DISCLOSURE_FALLBACK =
  'AI-generated 3D reconstruction from photos — geometry may be incomplete or inaccurate. Not a measured survey or a substitute for an in-person showing.';

const MOVE_KEYS = ['w', 'a', 's', 'd', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright'];

export default function WalkableSplatViewer({ splatUrl, disclosure, address, title, onClose }) {
  const canvasRef = useRef(null);
  const ctrlRef = useRef(null); // live controller state (mutated in the rAF loop)
  const [status, setStatus] = useState('loading'); // loading | ready | error | nowebgl
  const [progress, setProgress] = useState(0);
  const [hintVisible, setHintVisible] = useState(true);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !splatUrl) return undefined;

    let renderer;
    try {
      renderer = new SPLAT.WebGLRenderer(canvas);
    } catch {
      // Same degradation as PropertyCanvas: WebGL unavailable → CSS fallback.
      // One-shot flag flip on init failure; the effect returns immediately after.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setStatus('nowebgl');
      return undefined;
    }

    const scene = new SPLAT.Scene();
    const camera = new SPLAT.Camera();
    camera.data.setSize(canvas.clientWidth, canvas.clientHeight);

    // First-person controller state. yaw=0 looks toward +Z.
    const S = {
      pos: [0, 1.4, 3], yaw: Math.PI, pitch: 0,
      keys: new Set(), drag: false, lastX: 0, lastY: 0,
      joy: { x: 0, y: 0 }, bounds: null, speed: 0.04, ready: false,
    };
    ctrlRef.current = S;

    let raf = 0;
    let disposed = false;

    SPLAT.Loader.LoadAsync(absUrl(splatUrl), scene, (p) => setProgress(Math.round(p * 100)))
      .then((splat) => {
        if (disposed) return;
        const b = splat.bounds;
        const c = b.center();
        const sz = b.size();
        const ex = 0.25; // expand bounds 25% so the clamp is a safety net, not a cage
        S.bounds = {
          minx: b.min.x - sz.x * ex, maxx: b.max.x + sz.x * ex,
          miny: b.min.y, maxy: b.max.y,
          minz: b.min.z - sz.z * ex, maxz: b.max.z + sz.z * ex,
        };
        S.pos = [c.x, c.y, c.z];                       // start in the middle of the space
        S.speed = Math.max(0.01, Math.max(sz.x, sz.z) * 0.012); // scale walk speed to scene size
        S.ready = true;
        setStatus('ready');
      })
      .catch(() => { if (!disposed) setStatus('error'); });

    // ── input ──────────────────────────────────────────────────────────────
    const onKey = (down) => (e) => {
      const k = e.key.toLowerCase();
      if (!MOVE_KEYS.includes(k)) return;
      e.preventDefault();
      if (down) S.keys.add(k); else S.keys.delete(k);
    };
    const kd = onKey(true);
    const ku = onKey(false);
    window.addEventListener('keydown', kd);
    window.addEventListener('keyup', ku);

    // Drag-look from the canvas only (the joystick has its own handlers + stops
    // propagation), so look + move never fight. Pointer events unify mouse+touch.
    const pd = (e) => { S.drag = true; S.lastX = e.clientX; S.lastY = e.clientY; setHintVisible(false); };
    const pm = (e) => {
      if (!S.drag) return;
      S.yaw -= (e.clientX - S.lastX) * 0.005;
      S.pitch = Math.max(-1.35, Math.min(1.35, S.pitch - (e.clientY - S.lastY) * 0.005));
      S.lastX = e.clientX;
      S.lastY = e.clientY;
    };
    const pu = () => { S.drag = false; };
    canvas.addEventListener('pointerdown', pd);
    window.addEventListener('pointermove', pm);
    window.addEventListener('pointerup', pu);
    window.addEventListener('pointercancel', pu);

    const onResize = () => camera.data.setSize(canvas.clientWidth, canvas.clientHeight);
    window.addEventListener('resize', onResize);

    // ── render loop ──────────────────────────────────────────────────────────
    const render = () => {
      raf = requestAnimationFrame(render);
      if (S.ready) {
        let mf = 0; // forward (+1) / back (-1)
        let ms = 0; // strafe right (+1) / left (-1)
        if (S.keys.has('w') || S.keys.has('arrowup')) mf += 1;
        if (S.keys.has('s') || S.keys.has('arrowdown')) mf -= 1;
        if (S.keys.has('d') || S.keys.has('arrowright')) ms += 1;
        if (S.keys.has('a') || S.keys.has('arrowleft')) ms -= 1;
        mf += -S.joy.y; // joystick up = forward
        ms += S.joy.x;
        // forward(horizontal) = (sin yaw, 0, cos yaw); right = (cos yaw, 0, -sin yaw)
        const sy = Math.sin(S.yaw);
        const cy = Math.cos(S.yaw);
        S.pos[0] += (sy * mf + cy * ms) * S.speed;
        S.pos[2] += (cy * mf - sy * ms) * S.speed;
        const b = S.bounds;
        if (b) {
          S.pos[0] = Math.max(b.minx, Math.min(b.maxx, S.pos[0]));
          S.pos[1] = Math.max(b.miny, Math.min(b.maxy, S.pos[1]));
          S.pos[2] = Math.max(b.minz, Math.min(b.maxz, S.pos[2]));
        }
        const cp = Math.cos(S.pitch);
        camera.position = new SPLAT.Vector3(S.pos[0], S.pos[1], S.pos[2]);
        camera.target = new SPLAT.Vector3(
          S.pos[0] + Math.sin(S.yaw) * cp,
          S.pos[1] + Math.sin(S.pitch),
          S.pos[2] + Math.cos(S.yaw) * cp,
        );
      }
      try { renderer.render(scene, camera); } catch { /* transient context loss */ }
    };
    render();

    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      window.removeEventListener('keydown', kd);
      window.removeEventListener('keyup', ku);
      canvas.removeEventListener('pointerdown', pd);
      window.removeEventListener('pointermove', pm);
      window.removeEventListener('pointerup', pu);
      window.removeEventListener('pointercancel', pu);
      window.removeEventListener('resize', onResize);
      try { renderer.dispose(); } catch { /* noop */ }
      ctrlRef.current = null;
    };
  }, [splatUrl]);

  // ── on-screen joystick (touch) — writes normalized vector into the controller ──
  const joyRef = useRef(null);
  const onJoy = (e) => {
    e.stopPropagation();
    const el = joyRef.current;
    const S = ctrlRef.current;
    if (!el || !S) return;
    const r = el.getBoundingClientRect();
    const dx = (e.clientX - (r.left + r.width / 2)) / (r.width / 2);
    const dy = (e.clientY - (r.top + r.height / 2)) / (r.height / 2);
    const m = Math.hypot(dx, dy) || 1;
    const k = Math.min(1, m) / m;
    S.joy = { x: dx * k, y: dy * k };
  };
  const endJoy = (e) => { e.stopPropagation(); const S = ctrlRef.current; if (S) S.joy = { x: 0, y: 0 }; };

  return (
    <div className={styles.overlay} role="dialog" aria-modal="true" aria-label={`Walk inside ${title || address || 'property'}`}>
      <canvas ref={canvasRef} className={styles.canvas} />

      {status === 'loading' && (
        <div className={styles.center} aria-live="polite">
          <span className={styles.spinner} aria-hidden="true" />
          <span className={styles.loadingText}>Loading walkthrough… {progress}%</span>
        </div>
      )}
      {status === 'error' && (
        <div className={styles.center} role="alert">
          <p className={styles.errText}>Couldn’t load this 3D walkthrough.</p>
          <button type="button" className={styles.ghostBtn} onClick={onClose}>Close</button>
        </div>
      )}
      {status === 'nowebgl' && (
        <div className={styles.center} role="alert">
          <p className={styles.errText}>3D walkthroughs need WebGL, which is unavailable in this browser/session.</p>
          <button type="button" className={styles.ghostBtn} onClick={onClose}>Close</button>
        </div>
      )}

      <header className={styles.bar}>
        <span className={styles.kicker}>Neoh · Walk Inside</span>
        <span className={styles.addr} title={address}>{title || address}</span>
        <button type="button" className={styles.closeBtn} onClick={onClose} aria-label="Exit walkthrough">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
            <path d="M6 6l12 12M18 6 6 18" />
          </svg>
        </button>
      </header>

      {status === 'ready' && hintVisible && (
        <div className={styles.hint} role="status">Drag to look · W A S D to move · joystick on touch</div>
      )}

      {/* Touch joystick (hidden on fine-pointer via CSS) */}
      {status === 'ready' && (
        <div
          ref={joyRef}
          className={styles.joystick}
          onPointerDown={onJoy}
          onPointerMove={(e) => { if (e.buttons || e.pointerType === 'touch') onJoy(e); }}
          onPointerUp={endJoy}
          onPointerCancel={endJoy}
          aria-hidden="true"
        >
          <span className={styles.joyNub} />
        </div>
      )}

      {/* Mandatory AI-reconstruction disclosure — persistent, non-dismissible. */}
      <p className={styles.disclosure}>{disclosure || DISCLOSURE_FALLBACK}</p>
    </div>
  );
}
