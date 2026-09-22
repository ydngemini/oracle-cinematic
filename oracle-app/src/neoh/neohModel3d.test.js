import { describe, expect, it } from 'vitest';

import { PALETTE, buildLights, buildNeoh } from './neohModel3d';

/**
 * These tests exist because the head geometry was wrong four times running,
 * and every time it was the same class of mistake: a part positioned without
 * reference to the parts it has to sit against. The roof brimmed over the
 * skull, then buried itself inside it, then sliced its base plane straight
 * across the eyes.
 *
 * So rather than asserting coordinates — which would just restate the file
 * and break on every art tweak — these assert the RELATIONSHIPS that have to
 * hold for the model to read as a character at all.
 *
 * A stub `pc` keeps this on the CPU. buildNeoh only ever touches the engine
 * through the handful of constructors below, which is itself worth pinning:
 * the day it reaches for something else, this fails rather than the browser.
 */

function fakePc() {
  class Entity {
    constructor(name) {
      this.name = name;
      this.children = [];
      this.pos = [0, 0, 0];
      this.rot = [0, 0, 0];
      this.scale = [1, 1, 1];
      this.components = {};
    }
    addChild(c) { this.children.push(c); c.parent = this; }
    removeChild(c) { this.children = this.children.filter((x) => x !== c); }
    addComponent(name, opts) { this.components[name] = opts; return opts; }
    setLocalPosition(...v) { this.pos = v; }
    setLocalEulerAngles(...v) { this.rot = v; }
    setLocalScale(...v) { this.scale = v; }
    getLocalScale() { const [x, y, z] = this.scale; return { x, y, z, clone: () => ({ x, y, z }) }; }
  }
  const geom = (kind) => (device, opts) => ({ kind, opts, isMesh: true });
  return {
    Entity,
    Color: class { constructor(r, g, b) { Object.assign(this, { r, g, b }); } },
    Vec3: class { constructor(x, y, z) { Object.assign(this, { x, y, z }); } },
    MeshInstance: class { constructor(mesh, material) { Object.assign(this, { mesh, material }); } },
    StandardMaterial: class { update() { this.updated = true; } },
    CULLFACE_FRONT: 2,
    createSphere: geom('sphere'),
    createBox: geom('box'),
    createCylinder: geom('cylinder'),
    createTorus: geom('torus'),
    createCone: geom('cone'),
  };
}

const build = () => buildNeoh(fakePc(), { graphicsDevice: {} });

function walk(entity, out = []) {
  out.push(entity);
  for (const c of entity.children) walk(c, out);
  return out;
}

const byName = (root, name) => walk(root).find((e) => e.name === name);

describe('buildNeoh', () => {
  it('builds the parts the character is recognised by', () => {
    const { root } = build();
    const names = walk(root).map((e) => e.name);
    for (const part of ['skull', 'roof', 'roofRidge', 'visor', 'eye0', 'eye1', 'torso', 'neck']) {
      expect(names).toContain(part);
    }
  });

  it('returns the handles the renderer animates', () => {
    const { root, head, eyes, rings, emblem } = build();
    expect(root).toBeTruthy();
    expect(head).toBeTruthy();
    expect(eyes).toHaveLength(2);
    expect(rings).toHaveLength(2);
    expect(emblem).toBeTruthy();
  });

  it('turns the head independently of the body', () => {
    // The gaze reads as a look rather than a whole-body lurch only because
    // the head is its own node under the root.
    const { root, head } = build();
    expect(head.parent).toBe(root);
    expect(byName(root, 'skull').parent).toBe(head);
    expect(byName(root, 'torso').parent).toBe(root);
  });

  it('keeps the roof base clear of the visor', () => {
    // The bug: the roof's flat underside cutting a hard line across the eyes.
    const { root } = build();
    const roof = byName(root, 'roof');
    const visor = byName(root, 'visor');

    const roofBase = roof.pos[1] - roof.scale[1] / 2;
    const visorTop = visor.pos[1] + visor.scale[1] / 2;

    expect(roofBase).toBeGreaterThan(visorTop);
  });

  it('keeps both eyes below the roof base', () => {
    const { root, eyes } = build();
    const roof = byName(root, 'roof');
    const roofBase = roof.pos[1] - roof.scale[1] / 2;
    for (const eye of eyes) {
      expect(eye.pos[1] + eye.scale[1] / 2).toBeLessThan(roofBase);
    }
  });

  it('does not let the roof overhang the skull', () => {
    // The other bug, twice: a roof wider than the head, which stops being a
    // roof and becomes a hat brim. Rotated 45°, the roof's silhouette
    // half-width is baseRadius x scale x sin45.
    const { root } = build();
    const roof = byName(root, 'roof');
    const skull = byName(root, 'skull');

    const roofHalfWidth = 0.72 * roof.scale[0] * Math.SQRT1_2;
    const skullHalfWidth = 0.5 * skull.scale[0];

    expect(roofHalfWidth).toBeLessThanOrEqual(skullHalfWidth);
  });

  it('points a roof face at the camera, not an edge', () => {
    const { root } = build();
    expect(byName(root, 'roof').rot[1]).toBe(45);
  });

  it('gives the roof a true apex so the outline cannot show through', () => {
    const { root } = build();
    const mi = byName(root, 'roof').components.render.meshInstances[0];
    expect(mi.mesh.opts.peakRadius).toBe(0);
    expect(mi.mesh.opts.capSegments).toBe(4);
  });

  it('draws the ridge as an inverted hull larger than the roof', () => {
    const { root } = build();
    const roof = byName(root, 'roof');
    const ridge = byName(root, 'roofRidge');
    expect(ridge.scale[0]).toBeGreaterThan(roof.scale[0]);
    expect(ridge.components.render.meshInstances[0].material.cull).toBe(2);
  });

  it('passes mesh and material in that order to every MeshInstance', () => {
    // A swapped pair renders nothing and throws nowhere.
    const { root } = build();
    for (const e of walk(root)) {
      const mis = e.components.render?.meshInstances ?? [];
      for (const mi of mis) {
        expect(mi.mesh?.isMesh).toBe(true);
        expect(mi.material?.updated).toBe(true);
      }
    }
  });

  it('casts no shadows — there is no ground to catch them', () => {
    const { root } = build();
    for (const e of walk(root)) {
      if (e.components.render) expect(e.components.render.castShadows).toBe(false);
    }
  });
});

describe('PALETTE', () => {
  it('is frozen, and every channel is a 0..1 float', () => {
    expect(Object.isFrozen(PALETTE)).toBe(true);
    for (const rgb of Object.values(PALETTE)) {
      expect(rgb).toHaveLength(3);
      for (const c of rgb) expect(c).toBeGreaterThanOrEqual(0), expect(c).toBeLessThanOrEqual(1);
    }
  });
});

describe('buildLights', () => {
  it('adds a key and a fill, neither casting shadows', () => {
    const pc = fakePc();
    const root = new pc.Entity('root');
    const { key, fill } = buildLights(pc, root);
    expect(root.children).toContain(key);
    expect(root.children).toContain(fill);
    expect(key.components.light.castShadows).toBe(false);
    expect(fill.components.light.castShadows).toBe(false);
  });
});
