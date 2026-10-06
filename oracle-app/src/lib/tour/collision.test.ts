/**
 * Walk-mode collision from the splat itself, on synthetic point sets.
 *
 * The room: 6 × 4 (x × z) scene units, walls 2.5 high, split at x = 3 by a
 * 10 cm interior wall with a 1 m doorway at z ∈ [1.5, 2.5] (header above
 * 2.05). Eye height 1.55, so one scene unit is one nominal metre.
 */
import { describe, expect, it } from 'vitest';

import {
  buildOccupancyGrid,
  buildOccupancyGridAsync,
  collisionPlanForScene,
  colliderAppliesAt,
  createCollider,
  moveWithCollision,
  penetration,
  type CollisionPlan,
  type Collider,
  type SplatSamples,
} from './collision';
import { samplesFromGsplatData, samplesFromPlayCanvasResource, sogOpacityLut } from './splatSamples';
import { applySceneRenderModel, gsplatAntiAliasFor } from './sceneNavigation';
import { DEFAULT_CONFIG, createCameraState, createInputState, stepCamera } from './cameraModes';

// --- synthetic scenes ----------------------------------------------------------

/** Deterministic PRNG (mulberry32) so every run sees the same room. */
function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

class PointSet {
  xyz: number[] = [];
  alpha: number[] = [];
  private rand = rng(7);

  add(x: number, y: number, z: number, opacity = 0.9) {
    this.xyz.push(x, y, z);
    this.alpha.push(Math.round(opacity * 255));
  }

  /** Points on an axis-aligned rectangle; `axis` is the constant one. */
  plane(axis: 'x' | 'y' | 'z', at: number, a0: number, a1: number, b0: number, b1: number, perM2 = 400, opacity = 0.9) {
    const count = Math.round((a1 - a0) * (b1 - b0) * perM2);
    for (let i = 0; i < count; i += 1) {
      const a = a0 + (a1 - a0) * this.rand();
      const b = b0 + (b1 - b0) * this.rand();
      const j = (this.rand() - 0.5) * 0.04; // ±2 cm surface noise
      if (axis === 'x') this.add(at + j, a, b, opacity);       // a = y, b = z
      else if (axis === 'z') this.add(a, b, at + j, opacity);  // a = x, b = y
      else this.add(a, at + j, b, opacity);                     // a = x, b = z
    }
  }

  samples(): SplatSamples {
    return {
      centers: new Float32Array(this.xyz),
      opacityBytes: new Uint8Array(this.alpha),
      opacityStride: 1,
      opacityOffset: 0,
    };
  }
}

function room(options: { divider?: boolean } = {}): PointSet {
  const p = new PointSet();
  const H = 2.5;
  p.plane('y', 0, 0, 6, 0, 4);          // floor
  p.plane('y', H, 0, 6, 0, 4);          // ceiling
  p.plane('z', 0, 0, 6, 0, H);          // outer walls
  p.plane('z', 4, 0, 6, 0, H);
  p.plane('x', 0, 0, H, 0, 4);
  p.plane('x', 6, 0, H, 0, 4);
  if (options.divider !== false) {
    // Interior wall, both faces, doorway at z 1.5–2.5 with a header above 2.05.
    for (const face of [2.95, 3.05]) {
      p.plane('x', face, 0, H, 0, 1.5);
      p.plane('x', face, 0, H, 2.5, 4);
      p.plane('x', face, 2.05, H, 1.5, 2.5);
    }
  }
  return p;
}

const SCENE = {
  version: 2,
  floorHeight: 0,
  navigation: { eyeHeight: 1.55 },
  denseBounds: { min: [0, 0, 0] as [number, number, number], max: [6, 2.5, 4] as [number, number, number] },
};

function colliderFor(points: PointSet, scene = SCENE): { collider: Collider; plan: CollisionPlan } {
  const plan = collisionPlanForScene(scene);
  if (!plan) throw new Error('no plan');
  const outcome = buildOccupancyGrid([points.samples()], plan);
  if (!outcome.grid) throw new Error(`no grid: ${outcome.reason}`);
  return { collider: createCollider(outcome.grid, plan), plan };
}

/** Walk from `from` toward `to` in small frames, like the controller does. */
function walk(collider: Collider, from: [number, number], to: [number, number], stepLen = 0.04, frames = 600) {
  let pos: [number, number, number] = [from[0], 1.55, from[1]];
  for (let i = 0; i < frames; i += 1) {
    const dx = to[0] - pos[0];
    const dz = to[1] - pos[2];
    const d = Math.hypot(dx, dz);
    if (d < 1e-6) break;
    const k = Math.min(1, stepLen / d);
    pos = moveWithCollision(collider, pos, [pos[0] + dx * k, pos[1], pos[2] + dz * k]);
  }
  return pos;
}

// --- tests -----------------------------------------------------------------------

describe('collision plan', () => {
  it('needs a floor and an eye height — it never assumes metres', () => {
    expect(collisionPlanForScene(null)).toBeNull();
    expect(collisionPlanForScene({ floorHeight: 0 })).toBeNull();
    expect(collisionPlanForScene({ navigation: { eyeHeight: 1.5 } })).toBeNull();
    expect(collisionPlanForScene({ floorHeight: 0, navigation: { eyeHeight: 0 } })).toBeNull();
  });

  it('sizes everything from the capture’s own eye height', () => {
    const plan = collisionPlanForScene({ floorHeight: 0, navigation: { eyeHeight: 0.84 } });
    expect(plan).not.toBeNull();
    const unit = 0.84 / 1.55;
    expect(plan!.settings.cellSize).toBeCloseTo(0.1 * unit);
    expect(plan!.settings.bandLow).toBeCloseTo(0.3 * unit);
    expect(plan!.settings.bandHigh).toBeCloseTo(1.8 * unit);
    expect(plan!.settings.bodyRadius).toBeCloseTo(0.15 * unit);
  });
});

describe('a box room with one doorway', () => {
  const { collider } = colliderFor(room());

  it('marks the walls solid and the open floor free', () => {
    expect(penetration(collider.grid, 3.0, 0.8, 0.05)).toBeGreaterThan(0); // in the divider
    expect(penetration(collider.grid, 1.5, 2.0, 0.15)).toBe(0);            // mid-room
    expect(penetration(collider.grid, 3.0, 2.0, 0.15)).toBe(0);            // in the doorway
    expect(collider.grid.stats.opacity).toBe('per_splat');
  });

  it('cannot walk through the interior wall', () => {
    const end = walk(collider, [1.5, 0.8], [4.5, 0.8]);
    expect(end[0]).toBeLessThan(2.95);
    expect(end[0]).toBeGreaterThan(2.5); // walked right up to it
  });

  it('cannot walk out through an outer wall', () => {
    const end = walk(collider, [1.5, 2.0], [-1.5, 2.0]);
    expect(end[0]).toBeGreaterThan(0);
  });

  it('CAN walk through the doorway', () => {
    const end = walk(collider, [1.5, 2.0], [4.5, 2.0]);
    expect(end[0]).toBeCloseTo(4.5, 3);
    expect(end[2]).toBeCloseTo(2.0, 3);
  });

  it('slides along a wall instead of stopping dead', () => {
    // Heading mostly into the z = 0 wall, a little along it.
    const end = walk(collider, [1.0, 0.6], [2.0, -2.0], 0.04, 200);
    expect(end[2]).toBeGreaterThan(0.1);     // stayed out of the wall
    expect(end[0]).toBeGreaterThan(1.3);     // but kept moving along it
  });

  it('slides along a wall hit at a shallow diagonal', () => {
    const end = walk(collider, [0.5, 0.6], [2.5, -0.2], 0.04, 200);
    expect(end[2]).toBeGreaterThan(0.1);
    expect(end[0]).toBeGreaterThan(2.0);
  });

  it('does not tunnel through a wall on one huge frame', () => {
    const end = moveWithCollision(collider, [1.5, 1.55, 0.8], [4.5, 1.55, 0.8]);
    expect(end[0]).toBeLessThan(2.95);
  });

  it('lets someone who starts inside a wall walk out', () => {
    const end = walk(collider, [3.0, 0.8], [1.5, 0.8]);
    expect(end[0]).toBeCloseTo(1.5, 3);
  });

  it('never lets them go deeper in while escaping', () => {
    const start: [number, number, number] = [2.9, 1.55, 0.8]; // overlapping the wall face
    const before = penetration(collider.grid, start[0], start[2], collider.radius);
    expect(before).toBeGreaterThan(0);
    const end = moveWithCollision(collider, start, [3.0, 1.55, 0.8]);
    expect(penetration(collider.grid, end[0], end[2], collider.radius)).toBeLessThanOrEqual(before);
  });

  it('treats everything outside the grid as free (no data is not a wall)', () => {
    expect(penetration(collider.grid, 100, 100, 0.15)).toBe(0);
  });
});

describe('a plain wall next to detailed furniture', () => {
  it('still blocks when the furniture has 20x its Gaussians', () => {
    const p = new PointSet();
    p.plane('y', 0, 0, 6, 0, 4, 100);
    p.plane('z', 0, 0, 6, 0, 2.5, 100);
    p.plane('z', 4, 0, 6, 0, 2.5, 100);
    p.plane('x', 0, 0, 2.5, 0, 4, 100);
    p.plane('x', 6, 0, 2.5, 0, 4, 100);
    p.plane('x', 3.0, 0, 2.5, 0, 4, 100); // plain divider, no doorway
    // A richly textured bookcase against the far wall.
    for (const face of [5.4, 5.8]) p.plane('x', face, 0, 2.0, 0.5, 3.5, 2000);
    p.plane('z', 0.5, 5.4, 5.8, 0, 2.0, 2000);
    p.plane('z', 3.5, 5.4, 5.8, 0, 2.0, 2000);
    const { collider } = colliderFor(p);
    const end = walk(collider, [1.5, 2.0], [4.5, 2.0]);
    expect(end[0]).toBeLessThan(3.0);
  });
});

describe('what must NOT become a wall', () => {
  it('ignores an isolated floater', () => {
    const p = room({ divider: false });
    // A tight opaque clump with real vertical extent, right on the path.
    for (let i = 0; i < 200; i += 1) p.add(2.0 + (i % 5) * 0.01, 0.7 + (i / 200) * 0.6, 2.0 + (i % 3) * 0.01, 1);
    const { collider } = colliderFor(p);
    expect(collider.grid.stats.droppedComponents).toBeGreaterThan(0);
    const end = walk(collider, [0.8, 2.0], [4.0, 2.0]);
    expect(end[0]).toBeCloseTo(4.0, 3);
  });

  it('ignores translucent haze however dense', () => {
    const p = room({ divider: false });
    p.plane('x', 3.0, 0.3, 1.8, 0, 4, 2000, 0.1); // a sheet of 10%-opaque splats
    const { collider } = colliderFor(p);
    const end = walk(collider, [1.0, 2.0], [5.0, 2.0]);
    expect(end[0]).toBeCloseTo(5.0, 3);
  });

  it('ignores a thin horizontal sheet in the band (residual floor tilt, sagging ceiling)', () => {
    const p = room({ divider: false });
    p.plane('y', 0.6, 2.0, 4.0, 1.0, 3.0, 2000);
    const { collider } = colliderFor(p);
    const end = walk(collider, [1.0, 2.0], [5.0, 2.0]);
    expect(end[0]).toBeCloseTo(5.0, 3);
  });

  it('builds nothing from empty or sparse input', () => {
    const plan = collisionPlanForScene(SCENE)!;
    expect(buildOccupancyGrid([], plan)).toEqual({ grid: null, reason: 'no_samples' });
    expect(buildOccupancyGrid([{ centers: new Float32Array(0) }], plan).grid).toBeNull();
    const sparse = new PointSet();
    for (let i = 0; i < 100; i += 1) sparse.add(3, 1, i * 0.04);
    expect(buildOccupancyGrid([sparse.samples()], plan)).toEqual({ grid: null, reason: 'too_few_splats' });
  });

  it('refuses an implausible grid (what a wrong up axis looks like)', () => {
    // The room tipped on its side: floor and ceiling become "walls" filling
    // the whole footprint.
    const p = new PointSet();
    const rand = rng(3);
    for (let i = 0; i < 60_000; i += 1) p.add(rand() * 6, 0.3 + rand() * 1.5, rand() * 4);
    const plan = collisionPlanForScene(SCENE)!;
    expect(buildOccupancyGrid([p.samples()], plan)).toEqual({ grid: null, reason: 'implausible' });
  });
});

describe('frames, async build and the controller', () => {
  it('builds in the canonical frame and answers in the viewer’s source frame', () => {
    // Source frame is Z-up: canonical (x, y, z) = (sx, sz, -sy).
    const canon = room();
    const src = new PointSet();
    for (let i = 0; i < canon.xyz.length; i += 3) {
      src.add(canon.xyz[i], -canon.xyz[i + 2], canon.xyz[i + 1], canon.alpha[i / 3] / 255);
    }
    const M = [1, 0, 0, 0, 0, 0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1];
    const plan = collisionPlanForScene({ ...SCENE, canonicalTransform: M })!;
    const outcome = buildOccupancyGrid([src.samples()], plan);
    expect(outcome.grid).not.toBeNull();
    const collider = createCollider(outcome.grid!, plan, M);
    // Walking in the source frame: canonical z = -source y.
    const blocked = moveWithCollision(collider, [1.5, -0.8, 1.55], [4.5, -0.8, 1.55]);
    expect(blocked[0]).toBeLessThan(2.95);
  });

  it('builds the same grid in slices as in one go', async () => {
    const p = room();
    const plan = collisionPlanForScene(SCENE)!;
    plan.settings.chunkSize = 5000;
    let yields = 0;
    const asyncOutcome = await buildOccupancyGridAsync([p.samples()], plan, {
      yieldFn: async () => { yields += 1; },
    });
    const syncOutcome = buildOccupancyGrid([p.samples()], plan);
    expect(yields).toBeGreaterThan(5);
    expect(asyncOutcome.grid?.solid).toEqual(syncOutcome.grid?.solid);
  });

  it('stops building when the Space closes', async () => {
    const controller = new AbortController();
    controller.abort();
    const plan = collisionPlanForScene(SCENE)!;
    expect((await buildOccupancyGridAsync([room().samples()], plan, { signal: controller.signal })).reason)
      .toBe('aborted');
  });

  it('walk mode stops at a wall; orbit and other floors are unconstrained', () => {
    const { collider } = colliderFor(room());
    const cam = { setPosition: () => {}, setEulerAngles: () => {}, lookAt: () => {} };
    const state = createCameraState('walk');
    state.bounds = SCENE.denseBounds;
    state.position = [1.5, 1.55, 0.8];
    state.yaw = -Math.PI / 2; // forward = +x in stepCamera's convention
    state.collider = collider;
    const input = createInputState();
    input.keys.add('keyw');
    const config = { ...DEFAULT_CONFIG, eyeHeight: 1.55 };
    for (let i = 0; i < 200; i += 1) stepCamera(cam as never, state, input, 0.05, config);
    expect(state.position[0]).toBeLessThan(2.95);
    expect(state.position[0]).toBeGreaterThan(2.5);

    expect(colliderAppliesAt(collider, 0)).toBe(true);
    expect(colliderAppliesAt(collider, 3.0)).toBe(false);
    expect(colliderAppliesAt(null, 0)).toBe(false);
    state.floorY = 3.0; // upstairs: this grid is not for this floor
    for (let i = 0; i < 200; i += 1) stepCamera(cam as never, state, input, 0.05, config);
    expect(state.position[0]).toBeGreaterThan(3.05);
  });
});

describe('reading splats out of the engines', () => {
  it('decodes .sog opacity the way PlayCanvas does', () => {
    const v2 = sogOpacityLut({ version: 2 })!;
    expect(v2[0]).toBe(0);
    expect(v2[255]).toBe(1);
    const v1 = sogOpacityLut({ sh0: { mins: [0, 0, 0, -4], maxs: [0, 0, 0, 4] } })!;
    expect(v1[0]).toBeCloseTo(1 / (1 + Math.exp(4)));
    expect(v1[255]).toBeCloseTo(1 / (1 + Math.exp(-4)));
    expect(sogOpacityLut({ sh0: {} })).toBeNull();
    expect(sogOpacityLut(null)).toBeNull();
  });

  it('reads .sog opacity back from the sh0 texture, or falls back to centers alone', async () => {
    const centers = new Float32Array([0, 1, 0, 1, 1, 1]);
    const bytes = new Uint8Array([0, 0, 0, 200, 0, 0, 0, 50]);
    const sog = (read: unknown) => ({
      centers,
      gsplatData: { isSog: true, numSplats: 2, meta: { version: 2 }, sh0: { width: 2, height: 1, read } },
    });
    const ok = await samplesFromPlayCanvasResource(sog(() => Promise.resolve(bytes)));
    expect(ok?.opacityBytes).toBe(bytes);
    expect(ok?.opacityStride).toBe(4);
    expect(ok?.opacityOffset).toBe(3);
    const failed = await samplesFromPlayCanvasResource(sog(() => Promise.reject(new Error('no readback'))));
    expect(failed?.centers).toBe(centers);
    expect(failed?.opacityBytes).toBeUndefined();
    expect(await samplesFromPlayCanvasResource({ hasCenters: false })).toBeNull();
    expect(await samplesFromPlayCanvasResource(null)).toBeNull();
  });

  it('reads gsplat.js splat data (RGBA alpha is opacity)', () => {
    const s = samplesFromGsplatData({ positions: new Float32Array(6), colors: new Uint8Array(8), vertexCount: 2 });
    expect(s?.count).toBe(2);
    expect(s?.opacityStride).toBe(4);
    expect(samplesFromGsplatData({})).toBeNull();
  });
});

describe('render model', () => {
  it('draws anti-aliased only when the manifest says the scene was trained that way', () => {
    expect(gsplatAntiAliasFor({ renderModel: 'antialiased' })).toBe(true);
    expect(gsplatAntiAliasFor({ renderModel: 'classic' })).toBe(false);
    expect(gsplatAntiAliasFor({})).toBe(false);
    expect(gsplatAntiAliasFor(null)).toBe(false);
  });

  it('sets app.scene.gsplat.antiAlias from the manifest, and tolerates an engine without it', () => {
    const app = () => ({ scene: { gsplat: { antiAlias: undefined as boolean | undefined } } });
    const aa = app();
    expect(applySceneRenderModel(aa, { renderModel: 'antialiased' })).toBe(true);
    expect(aa.scene.gsplat.antiAlias).toBe(true);
    const classic = app();
    classic.scene.gsplat.antiAlias = true; // a previous scene's setting must not leak
    expect(applySceneRenderModel(classic, { renderModel: 'classic' })).toBe(false);
    expect(classic.scene.gsplat.antiAlias).toBe(false);
    const absent = app();
    expect(applySceneRenderModel(absent, null)).toBe(false);
    expect(absent.scene.gsplat.antiAlias).toBe(false);
    expect(applySceneRenderModel({ scene: {} }, { renderModel: 'antialiased' })).toBeNull();
    expect(applySceneRenderModel(null, { renderModel: 'antialiased' })).toBeNull();
  });
});
