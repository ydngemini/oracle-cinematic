/**
 * Walk-mode collision built from the splat itself.
 *
 * Why not the floor plan: derived floor-plan walls are unreliable on real
 * captures — gaps make rooms leak, clutter reads as wall — so colliding against
 * them would block real doorways and let people through real walls. The floor
 * is trustworthy; derived walls are not. What IS trustworthy about a wall is
 * that the reconstruction put dense, opaque Gaussians there. So this module
 * builds a coarse 2D occupancy grid in the floor plane from the splats that sit
 * in a body-height band above the floor, and the walk controller tests the
 * body against it.
 *
 * The rules, in order of importance:
 *   1. Never invent walls. If the grid cannot be built honestly (no splat
 *      data, no floor, no eye height, too few splats, an implausible result)
 *      the answer is `null` and the viewer keeps its old behaviour.
 *   2. Never trap anyone. A body that is already overlapping solid cells
 *      (spawned inside furniture, bad data) may always move to a position
 *      that is no deeper in, so it can always walk out.
 *   3. Slide, don't stop dead. A blocked step is replaced by the free step
 *      that makes the most progress in the intended direction.
 *
 * Pure maths: no PlayCanvas, no DOM. Everything is in scene units; physical
 * sizes are expressed in "nominal metres" derived from the capture's own eye
 * height (scene.json `navigation.eyeHeight`), because a reconstruction is not
 * in metres unless something measured it.
 */

/** Height (m) a capture's eye height is taken to represent, for sizing only. */
export const NOMINAL_EYE_HEIGHT_M = 1.55;

/** Physical defaults, in nominal metres (converted to scene units per scene). */
const NOMINAL = {
  /** Grid cell edge. 10 cm: fine enough for an 80 cm doorway to stay open. */
  cellSize: 0.10,
  /** Body band above the floor: above floor noise / rugs / thresholds … */
  bandLow: 0.30,
  /** … and below door headers, so a doorway is not closed off from above. */
  bandHigh: 1.80,
  /** Body radius. Small on purpose: an over-wide body closes real doorways. */
  bodyRadius: 0.15,
  /**
   * Minimum weighted standard deviation of splat heights within a cell. A
   * wall, a sofa back or a cabinet has vertical extent; a thin horizontal
   * sheet (residual floor tilt reaching the band, a sagging ceiling, a haze
   * layer) does not, and must not become a wall. 5 cm ~ 17 cm of extent.
   */
  minHeightSpread: 0.05,
};

export interface CollisionSettings {
  /** Cell edge, scene units. */
  cellSize: number;
  /** Floor plane height (scene up axis), scene units. */
  floorY: number;
  /** Body band, as heights above the floor, scene units. */
  bandLow: number;
  bandHigh: number;
  /** Body radius, scene units. */
  bodyRadius: number;
  /** Splats more transparent than this are ignored (haze, floaters). 0–1. */
  minOpacity: number;
  /** See NOMINAL.minHeightSpread; scene units. */
  minHeightSpread: number;
  /**
   * A cell is solid when its opacity-weighted splat count is at least
   * max(minCellWeight, relativeThreshold × p95 of occupied cells). Relative,
   * because splat density varies 20× between captures (100 k vs 2 M
   * Gaussians); the floor stops two or three stray splats ever being a wall.
   * Kept low (2%) because densification gives a detailed bookcase 10–20× the
   * Gaussians of a plain white wall, and the plain wall must still count.
   */
  relativeThreshold: number;
  minCellWeight: number;
  /** Area opening: solid 8-connected blobs smaller than this are dropped. */
  minComponentCells: number;
  /** Fewer band splats than this → no grid (not enough evidence). */
  minBandSplats: number;
  /**
   * More than this fraction of the walkable rectangle solid → no grid. That
   * is what a wrong up axis looks like (the floor becomes a wall), and a
   * grid that says "you can stand almost nowhere" is not believable.
   */
  maxSolidFraction: number;
  /** Cap on grid size; the cell grows to fit, and past 2× nominal we give up. */
  maxCells: number;
  /** Splats processed between yields in the async build. */
  chunkSize: number;
}

export const DEFAULT_COLLISION: Omit<CollisionSettings, 'cellSize' | 'floorY' | 'bandLow' | 'bandHigh' | 'bodyRadius' | 'minHeightSpread'> = {
  minOpacity: 0.3,
  relativeThreshold: 0.02,
  minCellWeight: 3,
  minComponentCells: 4,
  minBandSplats: 500,
  maxSolidFraction: 0.5,
  maxCells: 1_000_000,
  chunkSize: 65_536,
};

/** A rectangle in the floor plane (scene X / Z). */
export interface FloorRect {
  minX: number;
  minZ: number;
  maxX: number;
  maxZ: number;
}

/** The subset of scene.json collision needs. */
export interface CollisionSceneInfo {
  version?: number;
  canonicalTransform?: number[] | null;
  floorHeight?: number | null;
  denseBounds?: { min: [number, number, number]; max: [number, number, number] } | null;
  navigation?: { eyeHeight?: number | null } | null;
}

export interface CollisionPlan {
  settings: CollisionSettings;
  /** Area the grid covers; outside it nothing is solid. */
  area: FloorRect | null;
  /** The captured footprint (dense bounds) the plausibility check is taken over. */
  core: FloorRect | null;
  /** Row-major 4×4 taking splat (source) coordinates to the floor frame. */
  transform: number[] | null;
}

/**
 * Collision settings for a scene, or null when the scene does not say enough
 * to build them honestly: no floor height, or no eye height to size a body
 * by. Never assumes metres.
 *
 * `clampPadding` is how far beyond the dense bounds the viewer lets people
 * walk (cameraModes.clampToBounds); the grid covers that whole area.
 */
export function collisionPlanForScene(
  scene: CollisionSceneInfo | null | undefined,
  clampPadding = 1.0,
): CollisionPlan | null {
  if (!scene) return null;
  const eye = Number(scene.navigation?.eyeHeight);
  const floorY = scene.floorHeight;
  if (!(eye > 0) || typeof floorY !== 'number' || !Number.isFinite(floorY)) return null;
  const transform = Array.isArray(scene.canonicalTransform) && scene.canonicalTransform.length === 16
    && scene.canonicalTransform.every((v) => Number.isFinite(v))
    ? scene.canonicalTransform
    : null;
  const unit = eye / NOMINAL_EYE_HEIGHT_M; // scene units per nominal metre
  const settings: CollisionSettings = {
    ...DEFAULT_COLLISION,
    cellSize: NOMINAL.cellSize * unit,
    floorY,
    bandLow: NOMINAL.bandLow * unit,
    bandHigh: NOMINAL.bandHigh * unit,
    bodyRadius: NOMINAL.bodyRadius * unit,
    minHeightSpread: NOMINAL.minHeightSpread * unit,
  };
  const b = scene.denseBounds;
  const pad = clampPadding + settings.bodyRadius + settings.cellSize * 2;
  const area = b && b.min.length === 3 && b.max.length === 3 && b.max[0] > b.min[0] && b.max[2] > b.min[2]
    ? { minX: b.min[0] - pad, minZ: b.min[2] - pad, maxX: b.max[0] + pad, maxZ: b.max[2] + pad }
    : null;
  const core = area && b ? { minX: b.min[0], minZ: b.min[2], maxX: b.max[0], maxZ: b.max[2] } : null;
  return { settings, area, core, transform };
}

/**
 * One batch of splats. Centers are xyz triples. Opacity is optional and read
 * through a 256-entry lookup on one byte per splat, which is how both .sog
 * (sh0 alpha) and .splat (RGBA alpha) store it — no 8 MB float copy needed.
 */
export interface SplatSamples {
  centers: ArrayLike<number>;
  /** Defaults to centers.length / 3. */
  count?: number;
  opacityBytes?: ArrayLike<number> | null;
  /** Byte stride per splat in opacityBytes (4 for RGBA). */
  opacityStride?: number;
  /** Byte offset of the opacity byte within a stride (3 for RGBA alpha). */
  opacityOffset?: number;
  /** byte → opacity 0..1. Defaults to byte / 255. */
  opacityLut?: ArrayLike<number> | null;
}

export interface OccupancyGrid {
  originX: number;
  originZ: number;
  cellSize: number;
  cols: number;
  rows: number;
  /** 1 = solid, row-major (row = Z, col = X). */
  solid: Uint8Array;
  stats: OccupancyStats;
}

export interface OccupancyStats {
  splats: number;
  bandSplats: number;
  solidCells: number;
  solidFraction: number;
  threshold: number;
  droppedComponents: number;
  opacity: 'per_splat' | 'none';
}

export type GridOutcome =
  | { grid: OccupancyGrid; reason: null }
  | { grid: null; reason: 'no_samples' | 'too_few_splats' | 'grid_too_large' | 'implausible' | 'nothing_solid' | 'aborted' };

/** Generator so the sync and the chunked async build share one code path. */
function* buildSteps(
  sources: SplatSamples[],
  plan: CollisionPlan,
): Generator<void, GridOutcome, void> {
  const s = plan.settings;
  const m = plan.transform;
  const m0 = m ? m[0] : 1; const m1 = m ? m[1] : 0; const m2 = m ? m[2] : 0; const m3 = m ? m[3] : 0;
  const m4 = m ? m[4] : 0; const m5 = m ? m[5] : 1; const m6 = m ? m[6] : 0; const m7 = m ? m[7] : 0;
  const m8 = m ? m[8] : 0; const m9 = m ? m[9] : 0; const m10 = m ? m[10] : 1; const m11 = m ? m[11] : 0;

  const totalSplats = sources.reduce((n, src) => n + sampleCount(src), 0);
  if (totalSplats === 0) return { grid: null, reason: 'no_samples' };

  // --- area -----------------------------------------------------------------
  let area = plan.area;
  if (!area) {
    // No dense bounds: take the band splats' own extent. Floaters can inflate
    // it; the cell-size cap below is what keeps that honest.
    let minX = Infinity; let minZ = Infinity; let maxX = -Infinity; let maxZ = -Infinity;
    for (const src of sources) {
      const c = src.centers; const n = sampleCount(src);
      for (let i = 0; i < n; i += 1) {
        if (i > 0 && i % s.chunkSize === 0) yield;
        const x = c[i * 3]; const y = c[i * 3 + 1]; const z = c[i * 3 + 2];
        const wy = m4 * x + m5 * y + m6 * z + m7 - s.floorY;
        if (!(wy >= s.bandLow && wy <= s.bandHigh)) continue;
        const wx = m0 * x + m1 * y + m2 * z + m3;
        const wz = m8 * x + m9 * y + m10 * z + m11;
        if (wx < minX) minX = wx; if (wx > maxX) maxX = wx;
        if (wz < minZ) minZ = wz; if (wz > maxZ) maxZ = wz;
      }
      yield;
    }
    if (!(maxX > minX && maxZ > minZ)) return { grid: null, reason: 'too_few_splats' };
    const pad = s.bodyRadius + s.cellSize * 2;
    area = { minX: minX - pad, minZ: minZ - pad, maxX: maxX + pad, maxZ: maxZ + pad };
  }
  let cell = s.cellSize;
  let cols = Math.ceil((area.maxX - area.minX) / cell);
  let rows = Math.ceil((area.maxZ - area.minZ) / cell);
  if (cols * rows > s.maxCells) {
    cell *= Math.sqrt((cols * rows) / s.maxCells);
    if (cell > s.cellSize * 2) return { grid: null, reason: 'grid_too_large' };
    cols = Math.ceil((area.maxX - area.minX) / cell);
    rows = Math.ceil((area.maxZ - area.minZ) / cell);
  }
  const cells = cols * rows;
  const ox = area.minX; const oz = area.minZ;
  const inv = 1 / cell;

  // --- accumulate: weight, Σw·h, Σw·h² per cell ------------------------------
  const w = new Float32Array(cells);
  const wh = new Float32Array(cells);
  const whh = new Float32Array(cells);
  let bandSplats = 0;
  let anyOpacity = false;
  for (const src of sources) {
    const c = src.centers; const n = sampleCount(src);
    const ob = src.opacityBytes && src.opacityBytes.length > 0 ? src.opacityBytes : null;
    const stride = src.opacityStride ?? 1;
    const off = src.opacityOffset ?? 0;
    const lut = src.opacityLut ?? null;
    if (ob) anyOpacity = true;
    for (let start = 0; start < n; start += s.chunkSize) {
      const end = Math.min(n, start + s.chunkSize);
      for (let i = start; i < end; i += 1) {
        const x = c[i * 3]; const y = c[i * 3 + 1]; const z = c[i * 3 + 2];
        const h = m4 * x + m5 * y + m6 * z + m7 - s.floorY;
        // NaN fails both comparisons, so non-finite centers drop out here.
        if (!(h >= s.bandLow && h <= s.bandHigh)) continue;
        let o = 1;
        if (ob) {
          const byte = ob[i * stride + off];
          o = lut ? lut[byte] : byte / 255;
          if (!(o >= s.minOpacity)) continue;
        }
        const col = Math.floor((m0 * x + m1 * y + m2 * z + m3 - ox) * inv);
        const row = Math.floor((m8 * x + m9 * y + m10 * z + m11 - oz) * inv);
        if (col < 0 || col >= cols || row < 0 || row >= rows) continue;
        const k = row * cols + col;
        w[k] += o;
        wh[k] += o * h;
        whh[k] += o * h * h;
        bandSplats += 1;
      }
      yield;
    }
  }
  if (bandSplats < s.minBandSplats) return { grid: null, reason: 'too_few_splats' };

  // --- threshold ---------------------------------------------------------------
  let occupied = 0;
  for (let k = 0; k < cells; k += 1) if (w[k] > 0) occupied += 1;
  const weights = new Float32Array(occupied);
  for (let k = 0, j = 0; k < cells; k += 1) if (w[k] > 0) weights[j++] = w[k];
  weights.sort();
  const p95 = weights[Math.min(weights.length - 1, Math.floor(weights.length * 0.95))] ?? 0;
  const threshold = Math.max(s.minCellWeight, s.relativeThreshold * p95);
  const minVar = s.minHeightSpread * s.minHeightSpread;
  const solid = new Uint8Array(cells);
  for (let k = 0; k < cells; k += 1) {
    const wk = w[k];
    if (wk < threshold) continue;
    const mean = wh[k] / wk;
    const variance = whh[k] / wk - mean * mean;
    if (variance >= minVar) solid[k] = 1;
  }
  yield;

  // --- area opening: drop small 8-connected blobs (floaters) -------------------
  const dropped = dropSmallComponents(solid, cols, rows, s.minComponentCells);
  let solidCells = 0;
  for (let k = 0; k < cells; k += 1) solidCells += solid[k];
  if (solidCells === 0) return { grid: null, reason: 'nothing_solid' };
  // Plausibility over the captured footprint, not the padding around it.
  let coreCells = cells;
  let coreSolid = solidCells;
  const core = plan.core;
  if (core) {
    const c0 = Math.max(0, Math.floor((core.minX - ox) * inv));
    const c1 = Math.min(cols - 1, Math.floor((core.maxX - ox) * inv));
    const r0 = Math.max(0, Math.floor((core.minZ - oz) * inv));
    const r1 = Math.min(rows - 1, Math.floor((core.maxZ - oz) * inv));
    coreCells = Math.max(1, (c1 - c0 + 1) * (r1 - r0 + 1));
    coreSolid = 0;
    for (let r = r0; r <= r1; r += 1) for (let c = c0; c <= c1; c += 1) coreSolid += solid[r * cols + c];
  }
  const solidFraction = coreSolid / coreCells;
  if (solidFraction > s.maxSolidFraction) return { grid: null, reason: 'implausible' };

  return {
    grid: {
      originX: ox,
      originZ: oz,
      cellSize: cell,
      cols,
      rows,
      solid,
      stats: {
        splats: totalSplats,
        bandSplats,
        solidCells,
        solidFraction,
        threshold,
        droppedComponents: dropped,
        opacity: anyOpacity ? 'per_splat' : 'none',
      },
    },
    reason: null,
  };
}

function sampleCount(src: SplatSamples): number {
  const fromCenters = Math.floor((src.centers?.length ?? 0) / 3);
  return Math.max(0, Math.min(fromCenters, src.count ?? fromCenters));
}

/** Clears 8-connected solid components smaller than `minCells`; returns how many. */
function dropSmallComponents(solid: Uint8Array, cols: number, rows: number, minCells: number): number {
  if (minCells <= 1) return 0;
  const seen = new Uint8Array(solid.length);
  const queue = new Int32Array(solid.length);
  let dropped = 0;
  for (let start = 0; start < solid.length; start += 1) {
    if (!solid[start] || seen[start]) continue;
    let head = 0; let tail = 0;
    queue[tail++] = start; seen[start] = 1;
    while (head < tail) {
      const k = queue[head++];
      const r = (k / cols) | 0; const c = k - r * cols;
      for (let dr = -1; dr <= 1; dr += 1) {
        const rr = r + dr;
        if (rr < 0 || rr >= rows) continue;
        for (let dc = -1; dc <= 1; dc += 1) {
          const cc = c + dc;
          if (cc < 0 || cc >= cols) continue;
          const n = rr * cols + cc;
          if (solid[n] && !seen[n]) { seen[n] = 1; queue[tail++] = n; }
        }
      }
    }
    if (tail < minCells) {
      for (let i = 0; i < tail; i += 1) solid[queue[i]] = 0;
      dropped += 1;
    }
  }
  return dropped;
}

/** Build the grid synchronously (tests, small scenes). */
export function buildOccupancyGrid(sources: SplatSamples[], plan: CollisionPlan): GridOutcome {
  const it = buildSteps(sources, plan);
  for (;;) {
    const step = it.next();
    if (step.done) return step.value;
  }
}

/**
 * Build the grid in slices, yielding to the event loop between chunks so the
 * Space stays interactive while it builds. Resolves to the same outcome as
 * the sync build, or `aborted`.
 */
export async function buildOccupancyGridAsync(
  sources: SplatSamples[],
  plan: CollisionPlan,
  options: { signal?: AbortSignal; yieldFn?: () => Promise<void> } = {},
): Promise<GridOutcome> {
  const yieldFn = options.yieldFn ?? (() => new Promise<void>((resolve) => setTimeout(resolve, 0)));
  const it = buildSteps(sources, plan);
  for (;;) {
    if (options.signal?.aborted) return { grid: null, reason: 'aborted' };
    const step = it.next();
    if (step.done) return step.value;
    await yieldFn();
  }
}

// --- movement -------------------------------------------------------------------

/**
 * What the walk controller holds: the grid, the body, and how to get from the
 * viewer's frame into the grid's floor frame.
 */
export interface Collider {
  grid: OccupancyGrid;
  radius: number;
  /** Floor this grid was built for; other floors walk without it. */
  floorY: number;
  /**
   * Row-major 4×4 from the viewer's walking frame to the grid frame, when they
   * differ (the gsplat viewer walks in the splat's source frame). Null when
   * the viewer already walks in the grid frame (PlayCanvas applies the
   * canonical transform to the entity).
   */
  toGrid: number[] | null;
}

export function createCollider(
  grid: OccupancyGrid,
  plan: CollisionPlan,
  toGrid: number[] | null = null,
): Collider {
  return { grid, radius: plan.settings.bodyRadius, floorY: plan.settings.floorY, toGrid };
}

/**
 * How many solid cells a body of `radius` at grid-frame (x, z) overlaps.
 * Zero is free. Outside the grid nothing is solid — no data is not a wall.
 */
export function penetration(grid: OccupancyGrid, x: number, z: number, radius: number): number {
  const { originX, originZ, cellSize, cols, rows, solid } = grid;
  const c0 = Math.max(0, Math.floor((x - radius - originX) / cellSize));
  const c1 = Math.min(cols - 1, Math.floor((x + radius - originX) / cellSize));
  const r0 = Math.max(0, Math.floor((z - radius - originZ) / cellSize));
  const r1 = Math.min(rows - 1, Math.floor((z + radius - originZ) / cellSize));
  const r2 = radius * radius;
  let count = 0;
  for (let r = r0; r <= r1; r += 1) {
    const cz0 = originZ + r * cellSize;
    const nz = Math.max(cz0, Math.min(z, cz0 + cellSize)) - z;
    for (let c = c0; c <= c1; c += 1) {
      if (!solid[r * cols + c]) continue;
      const cx0 = originX + c * cellSize;
      const nx = Math.max(cx0, Math.min(x, cx0 + cellSize)) - x;
      if (nx * nx + nz * nz < r2) count += 1;
    }
  }
  return count;
}

/** The grid is for one floor; on another floor the walk is unconstrained. */
export function colliderAppliesAt(collider: Collider | null | undefined, floorY: number): collider is Collider {
  if (!collider) return false;
  return Math.abs(floorY - collider.floorY) <= Math.max(1e-6, collider.radius);
}

function penetrationAt(collider: Collider, x: number, y: number, z: number): number {
  const m = collider.toGrid;
  if (!m) return penetration(collider.grid, x, z, collider.radius);
  return penetration(
    collider.grid,
    m[0] * x + m[1] * y + m[2] * z + m[3],
    m[8] * x + m[9] * y + m[10] * z + m[11],
    collider.radius,
  );
}

/** Slide candidates: rotations of the blocked step, scaled by their projection. */
const SLIDE_ANGLES = [Math.PI / 6, -Math.PI / 6, Math.PI / 3, -Math.PI / 3];
/** Two axis slides, then the rotated ones. */
const SLIDE_CANDIDATES = 2 + SLIDE_ANGLES.length;
/** Substeps per frame are capped; past this a step is coarser but still tested. */
const MAX_SUBSTEPS = 256;

/**
 * Move from `from` toward `to` in the walk plane (X/Z of the viewer's frame),
 * stopping at solid cells and sliding along them. The returned Y is `to`'s.
 *
 * The step is cut into sub-steps of at most half a cell, so a fast frame
 * cannot tunnel through a one-cell wall.
 */
export function moveWithCollision(
  collider: Collider,
  from: readonly [number, number, number],
  to: readonly [number, number, number],
): [number, number, number] {
  const dx = to[0] - from[0];
  const dz = to[2] - from[2];
  const dist = Math.hypot(dx, dz);
  if (!(dist > 0)) return [to[0], to[1], to[2]];
  const n = Math.min(MAX_SUBSTEPS, Math.max(1, Math.ceil(dist / (collider.grid.cellSize * 0.5))));
  const sx = dx / n;
  const sz = dz / n;
  const y = to[1];
  let x = from[0];
  let z = from[2];
  let pen = penetrationAt(collider, x, y, z);
  // Inside solid already: any position no deeper in is allowed (never trap).
  const allowed = (p: number) => p === 0 || (pen > 0 && p <= pen);

  for (let i = 0; i < n; i += 1) {
    const p = penetrationAt(collider, x + sx, y, z + sz);
    if (allowed(p)) { x += sx; z += sz; pen = p; continue; }

    // Blocked: take the free step that makes the most progress along (sx, sz).
    let bestX = 0; let bestZ = 0; let bestPen = 0; let bestGain = 0;
    for (let c = 0; c < SLIDE_CANDIDATES; c += 1) {
      let cx: number; let cz: number;
      if (c === 0) { cx = sx; cz = 0; } else if (c === 1) { cx = 0; cz = sz; } else {
        const a = SLIDE_ANGLES[c - 2];
        const cos = Math.cos(a); const sin = Math.sin(a);
        cx = (sx * cos - sz * sin) * cos;
        cz = (sx * sin + sz * cos) * cos;
      }
      const gain = cx * sx + cz * sz;
      if (!(gain > bestGain)) continue;
      const cp = penetrationAt(collider, x + cx, y, z + cz);
      if (!allowed(cp)) continue;
      bestX = cx; bestZ = cz; bestPen = cp; bestGain = gain;
    }
    if (!(bestGain > 0)) break; // pushing straight into a wall: stop for this frame
    x += bestX;
    z += bestZ;
    pen = bestPen;
  }
  return [x, y, z];
}
