/**
 * Navigation tuned to the scene's own units, and honest about scale.
 *
 * A reconstruction is not in metres unless something measured it. The viewer
 * used to walk at a fixed "1.6 m" eye height and "2.4 m/s" — which, in a scene
 * whose units happen to be 0.3 m, put the camera through the ceiling and moved
 * it a room per second. scene.json (v2) records the height the capture was
 * actually taken at, in scene units; that is the right eye height whatever the
 * scale, and speeds follow from it.
 */

import { DEFAULT_CONFIG, type ControllerConfig } from './cameraModes';

export interface SceneScale {
  status?: 'metric' | 'estimated' | 'unknown';
  source?: string;
  metresPerUnit?: number | null;
  measurementsAllowed?: boolean;
  basis?: string;
}

export interface SceneNavigationInfo {
  navigation?: { eyeHeight?: number | null } | null;
  denseBounds?: { min: [number, number, number]; max: [number, number, number] } | null;
  scale?: SceneScale | null;
  limitations?: string[] | null;
}

/** Controller settings in scene units. Falls back to defaults when unknown. */
export function configForScene(scene: SceneNavigationInfo | null | undefined): ControllerConfig {
  const eye = Number(scene?.navigation?.eyeHeight);
  const bounds = scene?.denseBounds;
  if (!(eye > 0) && !bounds) return DEFAULT_CONFIG;
  const size = bounds
    ? Math.max(
      bounds.max[0] - bounds.min[0],
      bounds.max[1] - bounds.min[1],
      bounds.max[2] - bounds.min[2],
    )
    : 0;
  const eyeHeight = eye > 0 ? eye : DEFAULT_CONFIG.eyeHeight;
  // A comfortable indoor walk covers about 1.5 eye-heights per second.
  const ratio = eyeHeight / DEFAULT_CONFIG.eyeHeight;
  return {
    ...DEFAULT_CONFIG,
    eyeHeight,
    walkSpeed: DEFAULT_CONFIG.walkSpeed * ratio,
    minDistance: Math.max(0.01, DEFAULT_CONFIG.minDistance * ratio),
    maxDistance: size > 0 ? Math.max(size * 3, DEFAULT_CONFIG.minDistance * ratio * 4) : DEFAULT_CONFIG.maxDistance,
    minEyeAboveFloor: eyeHeight * 0.15,
  };
}

/** What the viewer may say about measurements — never more than is known. */
export function scaleNotice(scale: SceneScale | null | undefined): {
  measurementsAllowed: boolean;
  label: string;
} {
  if (scale?.status === 'metric' && scale.measurementsAllowed) {
    return { measurementsAllowed: true, label: 'Measured scale' };
  }
  if (scale?.status === 'estimated') {
    return { measurementsAllowed: false, label: 'Approximate scale — not for measuring' };
  }
  return { measurementsAllowed: false, label: 'Measurements unavailable — scale unknown' };
}

/** How the splats were trained to be drawn (scene.json `renderModel`). */
export type SplatRenderModel = 'antialiased' | 'classic';

/**
 * Whether the scene's splats must be drawn with PlayCanvas's anti-aliased
 * (mip-splatting style) projection. A scene trained with gsplat
 * `--antialiased` drawn in classic mode renders small splats too opaque.
 * Absent (spaces from before the field existed) means classic.
 */
export function gsplatAntiAliasFor(scene: { renderModel?: SplatRenderModel | null } | null | undefined): boolean {
  return scene?.renderModel === 'antialiased';
}

/**
 * Apply the scene's render model to a PlayCanvas app. Scene-wide in 2.21
 * (`app.scene.gsplat.antiAlias`); guarded because the unified gsplat params
 * may not exist on every engine build. Returns the value applied, or null
 * when there was nothing to apply it to.
 */
export function applySceneRenderModel(
  app: { scene?: { gsplat?: { antiAlias?: boolean } | null } | null } | null | undefined,
  scene: { renderModel?: SplatRenderModel | null } | null | undefined,
): boolean | null {
  const params = app?.scene?.gsplat;
  if (!params) return null;
  const antiAlias = gsplatAntiAliasFor(scene);
  params.antiAlias = antiAlias;
  return antiAlias;
}
