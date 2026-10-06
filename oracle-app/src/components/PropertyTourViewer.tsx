/**
 * PropertyTourViewer — PlayCanvas renderer for Neoh Space (phone-captured
 * property spaces).
 *
 * Accepts Gaussian splats as **.sog** (delivery format) or legacy **.splat**,
 * and mesh assets as .glb/.gltf. PLY is refused at the loader — it is training
 * output, roughly an order of magnitude larger for the same scene, and
 * shipping it to a phone stalls the tour before first paint.
 *
 * Device reality (docs/neoh-space.md): the caller passes the quality level the
 * device assessment chose; a frame-rate governor lowers the pixel ratio when
 * navigation stops being smooth; GPU context loss is caught and reported
 * through `onUnavailable` so the property page falls back instead of showing a
 * dead canvas. Navigation speeds and eye height come from scene.json's own
 * units, never an assumed "1.6 metres".
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
  createCameraState,
  createInputState,
  frameBounds,
  goToFloor,
  stepCamera,
  type CameraMode,
  type CameraState,
} from '../lib/tour/cameraModes';
import { type QualityLevel } from '../lib/tour/deviceCapability';
import { createFrameRateGovernor, pixelRatioFor } from '../lib/tour/renderQuality';
import {
  applySceneRenderModel,
  configForScene,
  scaleNotice,
  type SceneScale,
  type SplatRenderModel,
} from '../lib/tour/sceneNavigation';
import {
  buildOccupancyGridAsync,
  collisionPlanForScene,
  createCollider,
  type SplatSamples,
} from '../lib/tour/collision';
import { samplesFromPlayCanvasResource } from '../lib/tour/splatSamples';
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
  /** scene.json from the resolver: canonical up + where to open. Optional —
   *  a capture from before it existed frames its dense bounds instead. */
  scene?: SceneManifest | null;
  onClose?: () => void;
  embedded?: boolean;
  /** Starting render quality, from the device assessment. Default balanced. */
  quality?: QualityLevel;
  /** Called when this device cannot keep showing the space (no WebGL, GPU
   *  context lost and not restored). The caller shows its fallback. */
  onUnavailable?: (reason: string) => void;
}

/** The subset of scene.json the viewer reads. Written by backend/scene_manifest.py. */
export interface SceneManifest {
  version: number;
  /** Row-major 4x4; canonical = M * source. */
  canonicalTransform?: number[];
  denseBounds?: { min: [number, number, number]; max: [number, number, number] };
  floorHeight?: number | null;
  entryCamera?: {
    position: [number, number, number];
    target: [number, number, number];
    fov?: number;
  } | null;
  /** v2: what is known about real-world scale. */
  scale?: SceneScale | null;
  /** v2: eye height in scene units (the capture's own height). */
  navigation?: { eyeHeight?: number | null } | null;
  /** v2: deterministic caveats, e.g. camera_poses_missing. */
  limitations?: string[] | null;
  /** How the splats were trained to be drawn. Absent (older spaces) = classic. */
  renderModel?: SplatRenderModel | null;
}

type Status = 'idle' | 'loading' | 'ready' | 'error' | 'unsupported' | 'lost';

/**
 * Walk-mode walls (collision.ts). `none`: the scene does not say enough to
 * size a body or find the floor, so no walls are invented. `unavailable`:
 * tried and declined (no splat data, too few splats, implausible grid).
 */
type CollisionStatus = 'none' | 'building' | 'on' | 'unavailable';

/** Wait this long after the Space is interactive before building walls. */
const COLLISION_START_DELAY_MS = 750;

/** How long a lost GPU context may take to come back before we give up. */
const CONTEXT_RESTORE_GRACE_MS = 5000;

/**
 * Apply scene.json's canonical transform to a loaded entity.
 *
 * The manifest stores a row-major 4x4 with canonical = M * source; PlayCanvas
 * reads column-major, hence the transpose. Set as the entity's LOCAL transform,
 * so anything parented later inherits the same frame.
 */
function applyCanonicalTransform(pc: typeof pcNS, entity: pcNS.Entity, rowMajor: number[]): void {
  const colMajor: number[] = new Array(16);
  for (let r = 0; r < 4; r += 1) for (let c = 0; c < 4; c += 1) colMajor[c * 4 + r] = rowMajor[r * 4 + c];
  const mat = new pc.Mat4();
  mat.set(colMajor);
  const position = new pc.Vec3();
  const rotation = new pc.Quat();
  const scale = new pc.Vec3();
  mat.getTranslation(position);
  mat.getScale(scale);
  rotation.setFromMat4(mat);
  entity.setLocalPosition(position);
  entity.setLocalRotation(rotation);
  entity.setLocalScale(scale);
}

/**
 * Point the camera from `position` at `target`, in both modes' terms.
 *
 * Walk mode holds the eye; orbit mode holds the pivot and a distance. Yaw and
 * pitch follow stepCamera's convention: forward is
 * (sin yaw * cos pitch, sin pitch, cos yaw * cos pitch).
 */
function openAt(state: CameraState, entry: NonNullable<SceneManifest['entryCamera']>): void {
  const dx = entry.target[0] - entry.position[0];
  const dy = entry.target[1] - entry.position[1];
  const dz = entry.target[2] - entry.position[2];
  const len = Math.hypot(dx, dy, dz) || 1;
  state.yaw = Math.atan2(dx, dz);
  state.pitch = Math.asin(Math.max(-1, Math.min(1, dy / len)));
  if (state.mode === 'walk') {
    state.position = [entry.position[0], entry.position[1], entry.position[2]];
  } else {
    state.position = [entry.target[0], entry.target[1], entry.target[2]];
    state.distance = len;
  }
}

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

  // Poll animation frames, and always settle. The deadline is the point: a
  // frame callback does not run at all while the tab is hidden, so waiting on
  // one without a bound can hang forever — which is a worse failure than the
  // black screen this was meant to fix.
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
  scene = null,
  embedded = false,
  quality = 'balanced',
  onUnavailable,
}: PropertyTourViewerProps) {
  // Scene-unit navigation: eye height and speeds from scene.json, so walking
  // feels right whatever the reconstruction's units are.
  const navConfigRef = useRef(configForScene(scene));
  navConfigRef.current = configForScene(scene);
  const onUnavailableRef = useRef(onUnavailable);
  onUnavailableRef.current = onUnavailable;
  const [qualityLevel, setQualityLevel] = useState<QualityLevel>(quality);
  const scaleInfo = scaleNotice(scene?.scale);
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
  const [collision, setCollision] = useState<{ status: CollisionStatus; detail: string }>({ status: 'none', detail: '' });

  // --- engine lifecycle ----------------------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || assets.length === 0) return undefined;

    // Guards against the effect's async body touching a destroyed app if React
    // unmounts (or StrictMode double-invokes) mid-import.
    let disposed = false;
    const abort = new AbortController();
    let collisionTimer: ReturnType<typeof setTimeout> | null = null;

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
          setError('3D view unavailable on this device.');
          onUnavailableRef.current?.('no_webgl');
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
      // the single biggest cause of thermal throttling and OOM kills. The cap
      // follows the device's quality level and drops further if frames stall.
      app.graphicsDevice.maxPixelRatio = pixelRatioFor(quality, window.devicePixelRatio);

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

      // Draw the splats the way they were trained: an --antialiased capture in
      // classic mode renders small splats too opaque. Scene-wide, set before
      // any splat is added.
      applySceneRenderModel(app, scene);

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
        // Stand the capture up. The solver's frame is arbitrary; scene.json
        // holds the one transform every consumer applies, so the viewer,
        // the floor plan and anything structural later agree on which way
        // is up. Applied to the entity, not the bytes.
        if (scene?.canonicalTransform?.length === 16) {
          for (const item of loaded) applyCanonicalTransform(pc, item.entity, scene.canonicalTransform);
        }

        if (scene?.entryCamera) {
          // A real registered viewpoint, chosen for standing inside the space
          // with room in front. Deterministic: the same capture opens the same
          // way every time.
          //
          // NOTE the shape of this branch: it sets state and falls THROUGH to
          // the frame loop below. Returning early here skipped the loop that
          // applies camera state to the camera entity, so the camera stayed at
          // the origin and the canvas was black — the exact failure this
          // viewpoint exists to fix.
          openAt(cameraStateRef.current, scene.entryCamera);
          if (scene.denseBounds) cameraStateRef.current.bounds = scene.denseBounds;
          if (typeof scene.floorHeight === 'number') cameraStateRef.current.floorY = scene.floorHeight;
          if (scene.entryCamera.fov && cameraRef.current?.camera) {
            cameraRef.current.camera.fov = scene.entryCamera.fov;
          }
        } else if (scene?.denseBounds) {
          // Canonical, but no viewpoint worth opening at: frame what was
          // captured rather than its absolute extremes.
          frameBounds(cameraStateRef.current, scene.denseBounds);
        } else {
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
            setError('This 3D space loaded but could not be framed for viewing. Photos and listing details are still available on the property page.');
            return;
          }
          frameBounds(cameraStateRef.current, {
            min: [aabb.getMin().x, aabb.getMin().y, aabb.getMin().z],
            max: [aabb.getMax().x, aabb.getMax().y, aabb.getMax().z],
          });


        }

        setStatus('ready');
      } catch (err) {
        if ((err as DOMException)?.name === 'AbortError') return;
        if (disposed) return;
        setStatus('error');
        // The technical cause is for the console; the person gets plain words.
        // eslint-disable-next-line no-console
        console.warn('[neoh-space] load failed', err instanceof Error ? err.message : err);
        setError('This 3D space could not be loaded right now. Photos and listing details are still available on the property page.');
        return;
      }

      // Drive the camera from the engine's own frame loop so it stays in step
      // with rendering rather than fighting a separate rAF.
      const governor = createFrameRateGovernor(quality);
      const onUpdate = (dt: number) => {
        const cam = cameraRef.current;
        if (!cam) return;
        stepCamera(cam, cameraStateRef.current, inputRef.current, dt, navConfigRef.current);
        const next = governor.sample(dt);
        if (next) {
          app.graphicsDevice.maxPixelRatio = pixelRatioFor(next, window.devicePixelRatio);
          if (!disposed) setQualityLevel(next);
        }
      };
      app.on('update', onUpdate);

      // Walls, built from the splat itself once the Space is already
      // interactive: walking works (unconstrained) the moment it is ready,
      // and gains walls a moment later. Chunked so no frame stalls.
      const plan = collisionPlanForScene(scene);
      if (!plan) return;
      collisionTimer = setTimeout(() => {
        collisionTimer = null;
        void (async () => {
          if (disposed) return;
          setCollision({ status: 'building', detail: '' });
          const started = performance.now();
          const sources: SplatSamples[] = [];
          for (const item of loadedRef.current) {
            const samples = await samplesFromPlayCanvasResource(item.asset?.resource, { signal: abort.signal });
            if (samples) sources.push(samples);
          }
          if (disposed) return;
          const readMs = performance.now() - started;
          const outcome = await buildOccupancyGridAsync(sources, plan, { signal: abort.signal });
          if (disposed || outcome.reason === 'aborted') return;
          const ms = Math.round(performance.now() - started);
          if (!outcome.grid) {
            setCollision({ status: 'unavailable', detail: `${outcome.reason}; ${ms} ms` });
            return;
          }
          cameraStateRef.current.collider = createCollider(outcome.grid, plan);
          const st = outcome.grid.stats;
          setCollision({
            status: 'on',
            detail: `${ms} ms (read ${Math.round(readMs)} ms); ${st.splats} splats, ${st.bandSplats} in band, `
              + `${st.solidCells} solid of ${outcome.grid.cols}x${outcome.grid.rows}; opacity ${st.opacity}`,
          });
        })().catch(() => {
          if (!disposed) setCollision({ status: 'unavailable', detail: 'error' });
        });
      }, COLLISION_START_DELAY_MS);
    })();

    // GPU context loss (memory pressure, backgrounded tab on iOS, driver
    // reset). PlayCanvas restores its resources if the browser gives the
    // context back; if it does not within the grace period, the device cannot
    // keep showing this space and the caller falls back to photos.
    let restoreTimer: ReturnType<typeof setTimeout> | null = null;
    const onContextLost = (event: Event) => {
      event.preventDefault();
      if (disposed) return;
      setStatus('lost');
      setError('Reconnecting the 3D view…');
      restoreTimer = setTimeout(() => {
        if (disposed) return;
        setStatus('error');
        setError('3D view unavailable on this device.');
        onUnavailableRef.current?.('context_lost');
      }, CONTEXT_RESTORE_GRACE_MS);
    };
    const onContextRestored = () => {
      if (restoreTimer) { clearTimeout(restoreTimer); restoreTimer = null; }
      if (disposed) return;
      setError(null);
      setStatus('ready');
    };
    canvas.addEventListener('webglcontextlost', onContextLost);
    canvas.addEventListener('webglcontextrestored', onContextRestored);

    // --- teardown ----------------------------------------------------------
    // This is the part that matters on mobile. An Application that is not
    // destroyed keeps its WebGL context, every VRAM buffer, and its rAF loop
    // alive; a few open/close cycles of this drawer will OOM-kill the tab on
    // iOS. Order: stop loads → release assets → destroy app (which drops the
    // context, input devices, and frame loop) → null the refs.
    return () => {
      disposed = true;
      abort.abort();
      if (restoreTimer) clearTimeout(restoreTimer);
      if (collisionTimer) clearTimeout(collisionTimer);
      cameraStateRef.current.collider = null;
      canvas.removeEventListener('webglcontextlost', onContextLost);
      canvas.removeEventListener('webglcontextrestored', onContextRestored);

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
    // `quality` is read once at engine start; the governor owns it after.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      state.position = [state.position[0], state.floorY + navConfigRef.current.eyeHeight, state.position[2]];
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
      goToFloor(cameraStateRef.current, next, floors[next].y, navConfigRef.current);
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
    <div
      className={styles.overlay}
      role={embedded ? undefined : 'dialog'}
      aria-modal={embedded ? undefined : true}
      aria-label={title || 'Property tour'}
      data-space-status={status}
      data-space-quality={qualityLevel}
      data-space-collision={collision.status}
      data-space-collision-detail={collision.detail || undefined}
    >
      <canvas
        ref={canvasRef}
        className={styles.canvas}
        role="img"
        aria-label={`3D view of ${title || 'this property'}`}
        aria-describedby="neoh-space-controls-help"
      />
      <p id="neoh-space-controls-help" className={styles.srOnly}>
        Drag to look around. In Walk mode use W, A, S, D or the arrow keys to move;
        on a phone, use the joystick. Scroll or pinch to zoom in Orbit mode.
        Photos, the floor plan and listing details remain available on the property page.
      </p>

      {!embedded && <header className={styles.header}>
        <div className={styles.titleBlock}>
          {title ? <h2 className={styles.title}>{title}</h2> : null}
          {address ? <p className={styles.address}>{address}</p> : null}
        </div>
        {onClose ? (
          <button type="button" className={styles.close} onClick={onClose} aria-label="Close tour">×</button>
        ) : null}
      </header>}

      {status === 'loading' ? (
        <div className={styles.loading}>
          <div className={styles.progressTrack}>
            <div className={styles.progressFill} style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
          <p>Loading tour… {Math.round(progress * 100)}%</p>
        </div>
      ) : null}

      {status === 'error' || status === 'unsupported' || status === 'lost' ? (
        <div className={styles.error} role={status === 'lost' ? 'status' : 'alert'}>
          <p>{error || 'This tour could not be displayed.'}</p>
        </div>
      ) : null}

      {status === 'ready' && scene ? (
        <p className={styles.scaleNote} title={scene.scale?.basis || undefined}>
          {scaleInfo.label}
        </p>
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
                    goToFloor(cameraStateRef.current, index, floor.y, navConfigRef.current);
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
