import { useEffect, useRef } from 'react';

import { cornerPose } from './cornerPose';
import { buildLights, buildNeoh, GLOW_INTENSITY } from './neohModel3d';

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
 *
 * It shows the same product state as the flat mascot (T15.13): the corner
 * hands it the shared, already-resolved avatar state, and the pose comes
 * from cornerPose(state), which is derived from the same eye expressions the
 * SVG wears. The state lands in a ref the frame loop reads — a state change
 * never re-creates the engine, and React is never updated per frame.
 */

const YAW_RANGE = 26;   // degrees the head turns left/right at full deflection
const PITCH_RANGE = 15; // and up/down
const BLINK_EVERY = [2_600, 7_400]; // ms, randomised inside this window
/** Longest frame step the motion integrates; a tab waking from a stall must
 *  not fling the head across its whole range in one frame. */
const MAX_STEP_S = 0.1;

export function NeohCorner3D({
  onFailure, onReady, state = 'idle', actionType = 'generic', attentionLevel = 0, still = false,
}) {
  const canvasRef = useRef(null);
  // What to show, read by the frame loop. Synced in an effect (not during
  // render) for the same reason as the callbacks below.
  const poseRef = useRef({ pose: cornerPose(state), still });
  useEffect(() => {
    poseRef.current = { pose: cornerPose(state), still };
  }, [state, still]);
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

      const { root, head, eyes, glow } = buildNeoh(pc, app);
      app.root.addChild(root);
      buildLights(pc, app.root);
      // A touch of ambient so the shadowed side is soft grey, not black.
      app.scene.ambientLight = new pc.Color(0.42, 0.47, 0.55);

      // ── Motion ─────────────────────────────────────────────────────────
      // Autonomous drift, NOT pointer tracking. An avatar whose head follows
      // the cursor reads as surveillance rather than life, and it is the
      // fastest way to turn a calm character into a gimmick — so Neoh looks
      // around on his own schedule and has no idea where anyone is.
      //
      // Two incommensurable periods (11s and 7s) keep the path from settling
      // into a loop the eye can learn and start predicting.
      const target = { x: 0, y: 0 };
      const current = { x: 0, y: 0 };

      let elapsed = 0;
      let blinkAt = BLINK_EVERY[0];
      let blinkPhase = 0;
      // Read the eyes' rest scale from the model rather than restating it.
      // Hardcoding it here means the next tweak to eye size in neohModel3d.js
      // silently resizes them on the first blink and never restores.
      const eyeRest = eyes.map((e) => e.getLocalScale().clone());

      // The lights change colour only when the state does — a material
      // update per frame would cost more than the whole pose.
      let litState = '';
      const light = (pose) => {
        const [r, g, b] = pose.light;
        glow.diffuse = new pc.Color(r, g, b);
        glow.emissive = new pc.Color(r, g, b);
        glow.emissiveIntensity = GLOW_INTENSITY * pose.glow;
        glow.update();
        litState = pose.state;
      };
      // Eye shape eases toward the state's expression rather than snapping.
      const eye = { open: 1, width: 1 };

      app.on('update', (rawDt) => {
        const dt = Math.min(MAX_STEP_S, Math.max(0, rawDt));
        const { pose, still: holding } = poseRef.current;
        if (pose.state !== litState) light(pose);

        // Held still (hidden tab): the state's pose, no drift, no blink.
        if (!holding) elapsed += dt * 1000;
        const drift = holding ? 0 : pose.drift;
        const ease = holding ? 1 : Math.min(1, dt * 1.6);

        // Where he has decided to look, and the eased approach to it. The lag
        // is the point — an instant snap reads as a twitch, not attention.
        target.x = (Math.sin(elapsed / 11_000) * 0.55 + Math.sin(elapsed / 7_000) * 0.2) * drift - pose.gazeX;
        // Signs: a positive x yaws him to the viewer's left and a positive y
        // pitches him down, while the eye table's gaze is +x right, -y up.
        target.y = Math.sin(elapsed / 9_400) * 0.28 * drift + pose.gazeY;
        current.x += (target.x - current.x) * ease;
        current.y += (target.y - current.y) * ease;

        const breathe = holding ? 0 : Math.sin(elapsed / 1_450) * 0.055;
        const sway = holding ? 0 : Math.sin(elapsed / 2_900) * 2.2 * Math.max(0.3, drift);

        root.setLocalPosition(0, breathe, 0);
        root.setLocalEulerAngles(0, sway * 0.4, 0);
        head.setLocalEulerAngles(
          current.y * PITCH_RANGE + pose.pitch,
          current.x * -YAW_RANGE + sway,
          current.x * -2.4,
        );

        // Blink: squash the eyes on Y for a few frames, then restore. The
        // expression's own openness multiplies underneath it.
        if (!holding && elapsed > blinkAt) {
          blinkPhase = 1;
          blinkAt = elapsed + BLINK_EVERY[0] + Math.random() * (BLINK_EVERY[1] - BLINK_EVERY[0]);
        }
        if (blinkPhase > 0) blinkPhase = holding ? 0 : Math.max(0, blinkPhase - dt * 9);
        const shape = holding ? 1 : Math.min(1, dt * 8);
        eye.open += (pose.eyeOpen - eye.open) * shape;
        eye.width += (pose.eyeWidth - eye.width) * shape;
        const blink = 1 - Math.sin(blinkPhase * Math.PI) * 0.92;
        eyes.forEach((e, i) => {
          const rest = eyeRest[i];
          e.setLocalScale(rest.x * eye.width, rest.y * eye.open * blink, rest.z);
        });
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
  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      data-state={cornerPose(state).state}
      data-action={state === 'acting' ? actionType : undefined}
      data-attention={attentionLevel ? 'true' : 'false'}
      data-still={still ? 'true' : 'false'}
    />
  );
}

export default NeohCorner3D;
