import { useEffect, useRef } from 'react';

import { buildLights, buildNeoh } from './neohModel3d';

/**
 * The 3D corner Neoh — the heavy half.
 *
 * This module is the only thing in the app that pulls the PlayCanvas engine,
 * and it pulls it at runtime (`await import`) rather than at module scope, so
 * ~1.5 MB of renderer stays out of every bundle until something actually
 * decides to show a 3D Neoh. `PropertyTourViewer` already does exactly this,
 * so the engine chunk is shared rather than duplicated.
 *
 * It reports failure upward instead of rendering an empty box. A machine with
 * no WebGL, a lost context, or a chunk that failed to fetch should fall back
 * to the flat mascot, not to a hole in the corner.
 */

const YAW_RANGE = 26;   // degrees the head turns left/right at full deflection
const PITCH_RANGE = 15; // and up/down
const BLINK_EVERY = [2_600, 7_400]; // ms, randomised inside this window

export function NeohCorner3D({ onFailure, onReady }) {
  const canvasRef = useRef(null);
  // Callbacks live in a ref so a parent re-render cannot tear down the engine
  // — depending on them directly would rebuild the whole scene on any render.
  // Synced in an effect rather than during render: writing a ref while
  // rendering is impure, and this effect is declared first so it has already
  // run by the time the engine effect below looks at it.
  const cbRef = useRef({ onFailure, onReady });
  useEffect(() => {
    cbRef.current = { onFailure, onReady };
  });

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;

    let app = null;
    let disposed = false;
    const cleanups = [];

    (async () => {
      let pc;
      try {
        pc = await import('playcanvas');
      } catch {
        cbRef.current.onFailure?.('engine-unavailable');
        return;
      }
      if (disposed) return;

      try {
        app = new pc.Application(canvas, {
          graphicsDeviceOptions: {
            alpha: true,           // the page shows through behind him
            antialias: true,
            preserveDrawingBuffer: false,
            powerPreference: 'low-power',
          },
        });
      } catch {
        cbRef.current.onFailure?.('no-webgl');
        return;
      }
      if (disposed) { app.destroy(); app = null; return; }

      app.setCanvasFillMode(pc.FILLMODE_NONE);
      app.setCanvasResolution(pc.RESOLUTION_AUTO);

      const camera = new pc.Entity('camera');
      camera.addComponent('camera', {
        clearColor: new pc.Color(0, 0, 0, 0), // transparent, not a coloured card
        fov: 26,
        nearClip: 0.5,
        farClip: 60,
      });
      // Framed on the whole bust: he spans roughly y −1.7 (hands) to +2.2
      // (roof apex), so the camera sits at that midpoint, far enough back that
      // the peak has headroom rather than clipping the top of the canvas.
      camera.setLocalPosition(0, 0.34, 13.2);
      app.root.addChild(camera);

      const { root, head, eyes } = buildNeoh(pc, app);
      app.root.addChild(root);
      buildLights(pc, app.root);
      // A touch of ambient so the shadowed side is soft grey, not black.
      app.scene.ambientLight = new pc.Color(0.42, 0.47, 0.55);

      // ── Motion ─────────────────────────────────────────────────────────
      // Gaze is the pointer's position, smoothed. Nothing here invents
      // movement: with the pointer still and the tab focused, Neoh only
      // breathes and blinks.
      const target = { x: 0, y: 0 };
      const current = { x: 0, y: 0 };

      const onPointerMove = (event) => {
        target.x = (event.clientX / window.innerWidth) * 2 - 1;
        target.y = (event.clientY / window.innerHeight) * 2 - 1;
      };
      window.addEventListener('pointermove', onPointerMove, { passive: true });
      cleanups.push(() => window.removeEventListener('pointermove', onPointerMove));

      let elapsed = 0;
      let blinkAt = BLINK_EVERY[0];
      let blinkPhase = 0;
      // Read the eyes' rest scale from the model rather than restating it.
      // Hardcoding it here means the next tweak to eye size in neohModel3d.js
      // silently resizes them on the first blink and never restores.
      const eyeRest = eyes.map((e) => e.getLocalScale().clone());

      app.on('update', (dt) => {
        elapsed += dt * 1000;

        // Ease toward the pointer. The lag is the point — an instant snap
        // reads as a jump-scare, not attention.
        current.x += (target.x - current.x) * Math.min(1, dt * 3.4);
        current.y += (target.y - current.y) * Math.min(1, dt * 3.4);

        const breathe = Math.sin(elapsed / 1_450) * 0.055;
        const sway = Math.sin(elapsed / 2_900) * 2.2;

        root.setLocalPosition(0, breathe, 0);
        root.setLocalEulerAngles(0, sway * 0.4, 0);
        head.setLocalEulerAngles(
          current.y * PITCH_RANGE,
          current.x * -YAW_RANGE + sway,
          current.x * -2.4,
        );

        // Blink: squash the eyes on Y for a few frames, then restore.
        if (elapsed > blinkAt) {
          blinkPhase = 1;
          blinkAt = elapsed + BLINK_EVERY[0] + Math.random() * (BLINK_EVERY[1] - BLINK_EVERY[0]);
        }
        if (blinkPhase > 0) {
          blinkPhase = Math.max(0, blinkPhase - dt * 9);
          const open = 1 - Math.sin(blinkPhase * Math.PI) * 0.92;
          eyes.forEach((eye, i) => {
            const rest = eyeRest[i];
            eye.setLocalScale(rest.x, rest.y * open, rest.z);
          });
        }
      });

      // ── Sizing ─────────────────────────────────────────────────────────
      const resize = () => {
        const rect = canvas.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) app.resizeCanvas(rect.width, rect.height);
      };
      const ro = new ResizeObserver(resize);
      ro.observe(canvas);
      cleanups.push(() => ro.disconnect());

      // ── Stop rendering when nobody can see it ──────────────────────────
      // A hidden tab still runs rAF in some browsers, and an offscreen
      // canvas costs the same GPU as a visible one. Both are wasted heat.
      const io = new IntersectionObserver(([entry]) => {
        app.autoRender = entry.isIntersecting && document.visibilityState === 'visible';
      });
      io.observe(canvas);
      cleanups.push(() => io.disconnect());

      const onVisibility = () => {
        app.autoRender = document.visibilityState === 'visible';
      };
      document.addEventListener('visibilitychange', onVisibility);
      cleanups.push(() => document.removeEventListener('visibilitychange', onVisibility));

      // A lost context is not recoverable here; hand back to the flat mascot.
      const onContextLost = () => cbRef.current.onFailure?.('context-lost');
      canvas.addEventListener('webglcontextlost', onContextLost);
      cleanups.push(() => canvas.removeEventListener('webglcontextlost', onContextLost));

      app.start();
      resize();
      cbRef.current.onReady?.();
    })();

    return () => {
      disposed = true;
      for (const fn of cleanups) {
        try { fn(); } catch { /* teardown must not throw */ }
      }
      if (app) {
        try { app.destroy(); } catch { /* already gone */ }
      }
    };
  }, []);

  // aria-hidden for the same reason the flat mascot is: this is decoration,
  // and the surface already says what Neoh is doing in words.
  return <canvas ref={canvasRef} aria-hidden="true" />;
}

export default NeohCorner3D;
