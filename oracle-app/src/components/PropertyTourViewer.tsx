/**
 * PropertyTourViewer — PlayCanvas renderer for phone-captured property tours.
 *
 * Accepts Gaussian splats as **.splat** and mesh assets as .glb/.gltf. `.splat`
 * is the one splat format in this pipeline: the reconstruction worker emits it,
 * the tour resolver serves it, and both viewers read it. PLY is refused at the
 * loader — it is training output, roughly an order of magnitude larger for the
 * same scene, and shipping it to a phone stalls the tour before first paint.
 *
 * Runs ALONGSIDE the existing gsplat WalkableSplatViewer, selected by
 * VITE_TOUR_ENGINE. What this adds over that viewer: .glb mesh support (needed
 * by the floor-plan pipeline's 3D layout boxes), orbit mode, and multi-floor
 * navigation.
 *
 * PlayCanvas is imported lazily so it never lands in Oracle's initial bundle.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type * as pcNS from 'playcanvas';
import {
  loadTourAssets,
  releaseTourAsset,
  type LoadedTourAsset,
  type TourAssetSpec,
} from '../lib/tour/assetLoader';
import {
  DEFAULT_CONFIG,
  createCameraState,
  createInputState,
  frameBounds,
  goToFloor,
  stepCamera,
  type CameraMode,
} from '../lib/tour/cameraModes';
import styles from './PropertyTourViewer.module.css';

const AI_DISCLOSURE =
  'AI-generated 3D reconstruction from photos — geometry may be incomplete or ' +
  'inaccurate. Not a measured survey or a substitute for an in-person showing.';

export interface TourFloor {
  id: string;
  name: string;
  index: number;
  /** Floor plane height in metres. */
  y: number;
}

export interface PropertyTourViewerProps {
  assets: TourAssetSpec[];
  floors?: TourFloor[];
  initialMode?: CameraMode;
  address?: string;
  title?: string;
  /** Show the AI-reconstruction disclosure. Default true — pass false only for
   *  measured/manually-authored geometry. */
  aiGenerated?: boolean;
  /** Per-listing disclosure text from the tour resolver. Falls back to the
   *  generic AI disclosure — the specific honest badge must win when present. */
  disclosure?: string | null;
  onClose?: () => void;
}

type Status = 'idle' | 'loading' | 'ready' | 'error' | 'unsupported';

/** Fraction of splats trimmed from each end of each axis when framing. */
const FRAMING_TRIM = 0.02;
/** Enough samples for a stable percentile without sorting a million floats. */
const FRAMING_SAMPLE = 60_000;

/**
 * The box around where the splats actually ARE, not their absolute extremes.
 *
 * A real capture always has strays: background reconstructed through a window,
 * a handful of floaters behind the camera, points flung out by a bad match.
 * Framing on min/max lets a few of those set the scale, and the room they
 * surround becomes a speck in the middle of an empty view — which reads as a
 * broken reconstruction rather than a mis-aimed camera. Trimming a couple of
 * percent off each axis frames the part someone actually captured.
 */
function coreBounds(pc: typeof pcNS, centers: Float32Array): pcNS.BoundingBox | null {
  const count = Math.floor(centers.length / 3);
  if (count === 0) return null;
  const step = Math.max(1, Math.floor(count / FRAMING_SAMPLE));
  const xs: number[] = []; const ys: number[] = []; const zs: number[] = [];
  for (let i = 0; i < count; i += step) {
    const x = centers[i * 3]; const y = centers[i * 3 + 1]; const z = centers[i * 3 + 2];
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) continue;
    xs.push(x); ys.push(y); zs.push(z);
  }
  if (xs.length === 0) return null;
  const range = (values: number[]): [number, number] => {
    values.sort((a, b) => a - b);
    const lo = Math.floor(values.length * FRAMING_TRIM);
    const hi = Math.max(lo, Math.ceil(values.length * (1 - FRAMING_TRIM)) - 1);
    return [values[lo], values[hi]];
  };
  const [minX, maxX] = range(xs);
  const [minY, maxY] = range(ys);
  const [minZ, maxZ] = range(zs);
  if (!(minX <= maxX)) return null;
  const box = new pc.BoundingBox();
  box.setMinMax(new pc.Vec3(minX, minY, minZ), new pc.Vec3(maxX, maxY, maxZ));
  return box;
}

/**
 * The bounding box of one loaded tour asset.
 *
 * A gsplat's bounds cannot be read from `gsplat.instance`: the component
 * defaults to PlayCanvas's *unified* renderer, which never creates a
 * GSplatInstance at all, so that field is null by design and the engine's
 * non-unified path is documented as being removed. Reading it was why a
 * perfectly good 923k-splat capture rendered to a black canvas — the camera
 * was never aimed at anything.
 *
 * The splat centers are the honest source: they are the actual positions the
 * renderer draws, independent of which rendering path the engine chooses.
 */
function boundsOf(pc: typeof pcNS, item: { entity: pcNS.Entity; asset?: pcNS.Asset }): pcNS.BoundingBox | null {
  const resource = item.asset?.resource as any;
  const centers: Float32Array | undefined =
    resource?.hasCenters === false ? undefined : resource?.centers;
  if (centers && centers.length >= 3) {
    const box = coreBounds(pc, centers);
    if (box) return box;
  }
  // Meshes, and the legacy non-unified splat path, still report their own.
  const gsplat = (item.entity as any).gsplat;
  const render = (item.entity as any).render;
  return gsplat?.instance?.meshInstance?.aabb ?? render?.meshInstances?.[0]?.aabb ?? null;
}

/**
 * Wait for the loaded entities to report a bounding box.
 *
 * `gsplat.instance.meshInstance` does not exist the moment the asset finishes
 * loading; the engine creates it on a subsequent frame. Poll animation frames
 * until it appears, with a deadline so a genuinely boundless asset fails
 * visibly instead of hanging.
 */
async function waitForLoadedBounds(
  pc: typeof pcNS,
  loaded: Array<{ entity: pcNS.Entity }>,
  signal: AbortSignal,
  maxWaitMs = 10_000,
): Promise<pcNS.BoundingBox | null> {
  const read = (): pcNS.BoundingBox | null => {
    const aabb = new pc.BoundingBox();
    let initialised = false;
    for (const item of loaded) {
      const box = boundsOf(pc, item);
      if (!box) continue;
      if (!initialised) { aabb.copy(box); initialised = true; }
      else aabb.add(box);
    }
    return initialised ? aabb : null;
  };

  const immediate = read();
  if (immediate) return immediate;

  // requestAnimationFrame, not the engine's own 'update' event: that event
  // never fired here and the wait hung forever, which is a worse failure than
  // the black screen it was meant to fix. rAF ticks regardless of what the
  // engine is doing, and the deadline guarantees this settles either way.
  return new Promise((resolve) => {
    const deadline = performance.now() + maxWaitMs;
    const tick = () => {
      if (signal.aborted) { resolve(null); return; }
      const found = read();
      if (found) { resolve(found); return; }
      if (performance.now() >= deadline) { resolve(null); return; }
      window.requestAnimationFrame(tick);
    };
    window.requestAnimationFrame(tick);
  });
}

export default function PropertyTourViewer({
  assets,
  floors = [],
  initialMode = 'orbit',
  address,
  title,
  aiGenerated = true,
  disclosure,
  onClose,
}: PropertyTourViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  // Every mutable engine handle lives in a ref so the teardown effect can reach
  // it without re-running on state changes.
  const appRef = useRef<pcNS.Application | null>(null);
  const cameraRef = useRef<pcNS.Entity | null>(null);
  const loadedRef = useRef<LoadedTourAsset[]>([]);
  const cameraStateRef = useRef(createCameraState(initialMode));
  const inputRef = useRef(createInputState());

  const [status, setStatus] = useState<Status>('idle');
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<CameraMode>(initialMode);
  const [activeFloor, setActiveFloor] = useState(0);

  // --- engine lifecycle ----------------------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || assets.length === 0) return undefined;

    // Guards against the effect's async body touching a destroyed app if React
    // unmounts (or StrictMode double-invokes) mid-import.
    let disposed = false;
    const abort = new AbortController();

    (async () => {
      let pc: typeof pcNS;
      try {
        pc = await import('playcanvas');
      } catch {
        if (!disposed) setStatus('error');
        setError('Could not load the 3D engine.');
        return;
      }
      if (disposed) return;

      let app: pcNS.Application;
      try {
        app = new pc.Application(canvas, {
          graphicsDeviceOptions: {
            // Mobile GPUs: antialias is expensive and alpha forces a slower
            // compositing path. depth is required for correct splat sorting.
            antialias: false,
            alpha: false,
            depth: true,
            // Prevents the browser from silently dropping the context under
            // memory pressure without telling us.
            powerPreference: 'high-performance',
          },
          mouse: new pc.Mouse(canvas),
          touch: new pc.TouchDevice(canvas),
          keyboard: new pc.Keyboard(window),
        });
      } catch {
        if (!disposed) {
          setStatus('unsupported');
          setError('WebGL is unavailable on this device.');
        }
        return;
      }

      if (disposed) {
        app.destroy();
        return;
      }
      appRef.current = app;

      app.setCanvasFillMode(pc.FILLMODE_FILL_WINDOW);
      app.setCanvasResolution(pc.RESOLUTION_AUTO);
      // Cap DPR: a 3x-density phone rendering a splat at native resolution is
      // the single biggest cause of thermal throttling and OOM kills.
      app.graphicsDevice.maxPixelRatio = Math.min(window.devicePixelRatio, 2);

      const camera = new pc.Entity('tour-camera');
      camera.addComponent('camera', {
        clearColor: new pc.Color(0.05, 0.06, 0.08),
        fov: 55,
        nearClip: 0.05,
        farClip: 500,
      });
      app.root.addChild(camera);
      cameraRef.current = camera;

      const light = new pc.Entity('key-light');
      light.addComponent('light', { type: 'directional', intensity: 1.1 });
      light.setEulerAngles(45, 30, 0);
      app.root.addChild(light);

      app.start();
      setStatus('loading');

      try {
        const loaded = await loadTourAssets(pc, app, assets, {
          signal: abort.signal,
          onProgress: (fraction) => { if (!disposed) setProgress(fraction); },
        });
        if (disposed) {
          for (const item of loaded) releaseTourAsset(app, item);
          return;
        }
        loadedRef.current = loaded;

        // Frame whatever actually loaded — but its bounds do not exist yet.
        // A gsplat's meshInstance is built by the engine a frame or two AFTER
        // its asset resolves, so reading the box here returned undefined, the
        // framing was skipped, and the camera stayed at its default pose
        // looking at nothing. That renders as a black canvas, or from far
        // enough away as a sprinkle of dots — which reads as a broken splat
        // rather than as a camera that was never aimed.
        const aabb = await waitForLoadedBounds(pc, loaded, abort.signal);
        if (disposed || abort.signal.aborted) return;
        if (!aabb) {
          // Say WHICH part is missing. "No bounds" has several causes — no
          // component, an asset that never produced a resource, an instance
          // the engine never built — and they need different fixes.
          // eslint-disable-next-line no-console
          console.warn('[neoh-tour] no bounds', loaded.map((item) => {
            const g = (item.entity as any).gsplat;
            return {
              id: item.spec.id,
              kind: item.spec.kind ?? null,
              filename: item.spec.filename ?? null,
              hasGsplatComponent: !!g,
              assetLoaded: !!item.asset?.loaded,
              assetType: item.asset?.type ?? null,
              resource: item.asset?.resource ? item.asset.resource.constructor?.name : null,
              hasInstance: !!g?.instance,
              hasMeshInstance: !!g?.instance?.meshInstance,
            };
          }));
          // Never report ready with an unaimed camera: black is indis-
          // tinguishable from a failed reconstruction, and this one is real.
          setStatus('error');
          setError('The 3D capture loaded but never reported its bounds, so the camera could not be aimed at it.');
          return;
        }
        frameBounds(cameraStateRef.current, {
          min: [aabb.getMin().x, aabb.getMin().y, aabb.getMin().z],
          max: [aabb.getMax().x, aabb.getMax().y, aabb.getMax().z],
        });


        setStatus('ready');
      } catch (err) {
        if ((err as DOMException)?.name === 'AbortError') return;
        if (disposed) return;
        setStatus('error');
        setError(err instanceof Error ? err.message : 'Failed to load the tour.');
        return;
      }

      // Drive the camera from the engine's own frame loop so it stays in step
      // with rendering rather than fighting a separate rAF.
      const onUpdate = (dt: number) => {
        const cam = cameraRef.current;
        if (!cam) return;
        stepCamera(cam, cameraStateRef.current, inputRef.current, dt, DEFAULT_CONFIG);
      };
      app.on('update', onUpdate);
    })();

    // --- teardown ----------------------------------------------------------
    // This is the part that matters on mobile. An Application that is not
    // destroyed keeps its WebGL context, every VRAM buffer, and its rAF loop
    // alive; a few open/close cycles of this drawer will OOM-kill the tab on
    // iOS. Order: stop loads → release assets → destroy app (which drops the
    // context, input devices, and frame loop) → null the refs.
    return () => {
      disposed = true;
      abort.abort();

      const app = appRef.current;
      if (app) {
        try {
          for (const item of loadedRef.current) releaseTourAsset(app, item);
          loadedRef.current = [];
          app.off('update');
          // Explicitly drop the WebGL context. destroy() does this in current
          // PlayCanvas, but losing it deliberately makes the release
          // deterministic across engine versions and mobile drivers.
          // `gl` only exists on the WebGL backend (not WebGPU), and is not on
          // the base GraphicsDevice type — hence the narrow cast.
          const gl = (app.graphicsDevice as unknown as { gl?: WebGLRenderingContext })?.gl;
          const ext = gl?.getExtension('WEBGL_lose_context');
          app.destroy();
          ext?.loseContext();
        } catch {
          /* engine already gone — nothing left to free */
        }
      }
      appRef.current = null;
      cameraRef.current = null;
    };
    // Re-initialising on an `assets` identity change is intended: a different
    // property means a different scene. Callers should memoise the array.
  }, [assets]);

  // --- input ---------------------------------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || status !== 'ready') return undefined;

    const input = inputRef.current;
    const activePointers = new Map<number, { x: number; y: number }>();
    let pinchDistance = 0;

    const onKeyDown = (e: KeyboardEvent) => {
      const code = e.code.toLowerCase();
      if (code === 'shiftleft' || code === 'shiftright') { input.running = true; return; }
      // Hot-keys: F = first-person walk, O = orbit, [ / ] = floor down/up.
      if (code === 'keyf') { switchMode('walk'); return; }
      if (code === 'keyo') { switchMode('orbit'); return; }
      if (code === 'bracketleft') { stepFloor(-1); return; }
      if (code === 'bracketright') { stepFloor(1); return; }
      if (code === 'escape') { onClose?.(); return; }
      input.keys.add(code);
    };
    const onKeyUp = (e: KeyboardEvent) => {
      const code = e.code.toLowerCase();
      if (code === 'shiftleft' || code === 'shiftright') input.running = false;
      input.keys.delete(code);
    };
    // A blur while keys are held would otherwise leave the camera drifting
    // forever after the user alt-tabs away.
    const onBlur = () => { input.keys.clear(); input.running = false; };

    const onPointerDown = (e: PointerEvent) => {
      canvas.setPointerCapture(e.pointerId);
      activePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    };
    const onPointerMove = (e: PointerEvent) => {
      const prev = activePointers.get(e.pointerId);
      if (!prev) return;
      if (activePointers.size === 1) {
        input.lookDelta.x += e.clientX - prev.x;
        input.lookDelta.y += e.clientY - prev.y;
      }
      activePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

      if (activePointers.size === 2) {
        const [a, b] = Array.from(activePointers.values());
        const distance = Math.hypot(a.x - b.x, a.y - b.y);
        if (pinchDistance > 0) input.zoomDelta += (pinchDistance - distance) * 0.02;
        pinchDistance = distance;
      }
    };
    const onPointerUp = (e: PointerEvent) => {
      activePointers.delete(e.pointerId);
      if (activePointers.size < 2) pinchDistance = 0;
      try { canvas.releasePointerCapture(e.pointerId); } catch { /* already released */ }
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      input.zoomDelta += e.deltaY * 0.01;
    };

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    window.addEventListener('blur', onBlur);
    canvas.addEventListener('pointerdown', onPointerDown);
    canvas.addEventListener('pointermove', onPointerMove);
    canvas.addEventListener('pointerup', onPointerUp);
    canvas.addEventListener('pointercancel', onPointerUp);
    canvas.addEventListener('wheel', onWheel, { passive: false });

    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
      window.removeEventListener('blur', onBlur);
      canvas.removeEventListener('pointerdown', onPointerDown);
      canvas.removeEventListener('pointermove', onPointerMove);
      canvas.removeEventListener('pointerup', onPointerUp);
      canvas.removeEventListener('pointercancel', onPointerUp);
      canvas.removeEventListener('wheel', onWheel);
      input.keys.clear();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, onClose]);

  const switchMode = useCallback((next: CameraMode) => {
    const state = cameraStateRef.current;
    if (state.mode === next) return;
    state.mode = next;
    if (next === 'walk') {
      // Drop the eye onto the current floor at the orbit pivot.
      state.position = [state.position[0], state.floorY + DEFAULT_CONFIG.eyeHeight, state.position[2]];
      state.pitch = 0;
    } else {
      state.pitch = -0.35;
    }
    setMode(next);
  }, []);

  const stepFloor = useCallback((delta: number) => {
    if (floors.length === 0) return;
    setActiveFloor((current) => {
      const next = Math.max(0, Math.min(floors.length - 1, current + delta));
      goToFloor(cameraStateRef.current, next, floors[next].y, DEFAULT_CONFIG);
      return next;
    });
  }, [floors]);

  // --- touch joystick ------------------------------------------------------
  const joystickRef = useRef<HTMLDivElement | null>(null);
  const onJoystickMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const el = joystickRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const radius = rect.width / 2;
    const dx = (e.clientX - cx) / radius;
    const dy = (e.clientY - cy) / radius;
    const magnitude = Math.hypot(dx, dy);
    const scale = magnitude > 1 ? 1 / magnitude : 1;
    inputRef.current.joystick = { x: dx * scale, y: -dy * scale };
  }, []);
  const onJoystickRelease = useCallback(() => {
    inputRef.current.joystick = { x: 0, y: 0 };
  }, []);

  return (
    <div className={styles.overlay} role="dialog" aria-modal="true" aria-label={title || 'Property tour'}>
      <canvas ref={canvasRef} className={styles.canvas} />

      <header className={styles.header}>
        <div className={styles.titleBlock}>
          {title ? <h2 className={styles.title}>{title}</h2> : null}
          {address ? <p className={styles.address}>{address}</p> : null}
        </div>
        {onClose ? (
          <button type="button" className={styles.close} onClick={onClose} aria-label="Close tour">×</button>
        ) : null}
      </header>

      {status === 'loading' ? (
        <div className={styles.loading}>
          <div className={styles.progressTrack}>
            <div className={styles.progressFill} style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
          <p>Loading tour… {Math.round(progress * 100)}%</p>
        </div>
      ) : null}

      {status === 'error' || status === 'unsupported' ? (
        <div className={styles.error} role="alert">
          <p>{error || 'This tour could not be displayed.'}</p>
        </div>
      ) : null}

      {status === 'ready' ? (
        <>
          <div className={styles.modeToggle} role="group" aria-label="Camera mode">
            <button
              type="button"
              className={mode === 'orbit' ? styles.modeActive : styles.modeButton}
              onClick={() => switchMode('orbit')}
              aria-pressed={mode === 'orbit'}
            >
              Orbit <kbd>O</kbd>
            </button>
            <button
              type="button"
              className={mode === 'walk' ? styles.modeActive : styles.modeButton}
              onClick={() => switchMode('walk')}
              aria-pressed={mode === 'walk'}
            >
              Walk <kbd>F</kbd>
            </button>
          </div>

          {floors.length > 1 ? (
            <div className={styles.floorNav} role="group" aria-label="Floor">
              {floors.map((floor, index) => (
                <button
                  key={floor.id}
                  type="button"
                  className={index === activeFloor ? styles.floorActive : styles.floorButton}
                  onClick={() => {
                    setActiveFloor(index);
                    goToFloor(cameraStateRef.current, index, floor.y, DEFAULT_CONFIG);
                  }}
                  aria-pressed={index === activeFloor}
                >
                  {floor.name}
                </button>
              ))}
            </div>
          ) : null}

          <div
            ref={joystickRef}
            className={styles.joystick}
            onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); onJoystickMove(e); }}
            onPointerMove={(e) => { if (e.buttons || e.pointerType === 'touch') onJoystickMove(e); }}
            onPointerUp={onJoystickRelease}
            onPointerCancel={onJoystickRelease}
            aria-hidden="true"
          >
            <span className={styles.joystickKnob} />
          </div>
        </>
      ) : null}

      {aiGenerated ? <p className={styles.disclosure}>{disclosure || AI_DISCLOSURE}</p> : null}
    </div>
  );
}
