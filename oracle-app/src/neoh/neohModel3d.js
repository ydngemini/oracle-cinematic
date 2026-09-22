/**
 * Neoh, as actual geometry.
 *
 * The character sheet is a flat render — there is no mesh inside a PNG to
 * extract — so this rebuilds him from primitives instead: a four-sided roof
 * over a rounded shell, a dark visor, two lit eyes, ear pods, and a torso
 * carrying the house emblem. Every colour below is sampled from that render
 * rather than guessed, so the model reads as the same character even though
 * it is not the same pixels.
 *
 * It takes the `pc` module rather than importing it, because PlayCanvas is
 * ~1.5 MB and must stay inside the dynamic chunk that loads this file. A
 * static import here would pull the whole engine into the main bundle.
 *
 * Pure builder: no React, no DOM, no timers. It returns the handles the
 * renderer animates, and knows nothing about why it is being moved.
 */

/** Sampled from the character sheet (median-cut over the hero figure). */
export const PALETTE = Object.freeze({
  shell: [0.969, 0.980, 0.988],   // #f7fafc — glossy white body
  shellShadow: [0.847, 0.855, 0.875], // #d8dadf — underside
  visor: [0.055, 0.125, 0.200],   // #0e2033 — the dark face
  rim: [0.176, 0.306, 0.408],     // #2d4e68 — mid blue-grey edging
  roofEdge: [0.471, 0.698, 0.784], // #78b2c8 — roof ridge
  glow: [0.157, 0.882, 0.969],    // #28e1f7 — eyes, emblem, ear rings
  joint: [0.227, 0.259, 0.314],   // charcoal joints and hands
});

const col = (pc, [r, g, b]) => new pc.Color(r, g, b);

/**
 * A glossy opaque material. `useMetalness` off keeps the toy-plastic look of
 * the reference; the metalness workflow makes white shells read as grey.
 */
function shellMaterial(pc, rgb, { gloss = 0.82 } = {}) {
  const m = new pc.StandardMaterial();
  m.diffuse = col(pc, rgb);
  m.gloss = gloss;
  m.useMetalness = false;
  m.update();
  return m;
}

/** Self-lit material for anything that should read as emitting light. */
function glowMaterial(pc, rgb, intensity = 1.5) {
  const m = new pc.StandardMaterial();
  m.diffuse = col(pc, rgb);
  m.emissive = col(pc, rgb);
  m.emissiveIntensity = intensity;
  m.useLighting = false;
  m.update();
  return m;
}

/**
 * The cyan line around the roof. Drawn as an inverted hull — the same cone,
 * scaled up a hair, with front faces culled so only its inside back faces
 * survive. That reads as a clean outline from every angle, which a second
 * scaled-up solid would not.
 */
function outlineMaterial(pc, rgb) {
  const m = glowMaterial(pc, rgb, 1.1);
  m.cull = pc.CULLFACE_FRONT;
  m.update();
  return m;
}

function meshEntity(pc, app, mesh, material, { name, pos, rot, scale }) {
  const e = new pc.Entity(name);
  const mi = new pc.MeshInstance(mesh, material);
  e.addComponent('render', { meshInstances: [mi], castShadows: false, receiveShadows: false });
  if (pos) e.setLocalPosition(...pos);
  if (rot) e.setLocalEulerAngles(...rot);
  if (scale) e.setLocalScale(...scale);
  return e;
}

/**
 * Build the bust — head, ears, neck, torso, arms.
 *
 * Legs are deliberately absent: at the ~200px this renders at, a full figure
 * turns the head into about thirty pixels, and the head is the part that
 * carries the character.
 *
 * @returns {{root: object, head: object, eyes: object[], rings: object[], emblem: object}}
 */
export function buildNeoh(pc, app) {
  const device = app.graphicsDevice;

  const shell = shellMaterial(pc, PALETTE.shell);
  const shellLow = shellMaterial(pc, PALETTE.shellShadow, { gloss: 0.6 });
  const visorMat = shellMaterial(pc, PALETTE.visor, { gloss: 0.94 });
  const jointMat = shellMaterial(pc, PALETTE.joint, { gloss: 0.5 });
  const glowMat = glowMaterial(pc, PALETTE.glow, 1.8);
  const ridgeMat = outlineMaterial(pc, PALETTE.roofEdge);

  const sphere = pc.createSphere(device, { radius: 0.5, latitudeBands: 24, longitudeBands: 24 });
  const box = pc.createBox(device, { halfExtents: new pc.Vec3(0.5, 0.5, 0.5) });
  const cylinder = pc.createCylinder(device, { radius: 0.5, height: 1, capSegments: 20 });
  const torus = pc.createTorus(device, { tubeRadius: 0.08, ringRadius: 0.42, segments: 20, sides: 12 });
  // capSegments: 4 turns the cone into a four-sided pyramid — the roof.
  // peakRadius 0 gives a true point. Anything above zero leaves a small flat
  // cap, and the outline hull behind it shows through the gap as a dark speck
  // right on the apex — the most conspicuous pixel on the whole model.
  const pyramid = pc.createCone(device, { baseRadius: 0.72, peakRadius: 0, height: 1, capSegments: 4, heightSegments: 1 });

  const root = new pc.Entity('neoh');

  // ── Head ──────────────────────────────────────────────────────────────
  // Its own node so the whole head can turn toward the pointer while the
  // torso stays put, which is what makes the gaze read as a look and not a
  // whole-body lurch.
  const head = new pc.Entity('head');
  head.setLocalPosition(0, 1.22, 0);
  root.addChild(head);

  // The head is a stack solved against one number: the roof's base plane,
  // y = 0.35. Three constraints meet there, and every earlier pass broke one:
  //
  //   · the base must be WIDE enough to meet the skull's own silhouette. Any
  //     narrower and its bottom edge floats inside the head, which reads as a
  //     party hat perched on a ball rather than the top of his head.
  //   · the base must sit near the skull's widest point (y = 0) so the flat
  //     underside is swallowed. Higher up the curve and it overhangs as a brim.
  //   · the visor needs real height beneath it. Squeezed flat it reads as a
  //     letterbox slot rather than a face, so the skull is tall (2.6) to make
  //     room for both rather than forcing them to compete.
  //   · the visor must clear it entirely, or the base plane slices the eyes.
  //
  // Skull half-width is 1.16 at y = 0 and 1.117 at the base plane.
  head.addChild(meshEntity(pc, app, sphere, shell, {
    name: 'skull', scale: [2.32, 2.6, 2.06],
  }));

  // Roof: rotated 45° so a flat face — not an edge — points at the camera.
  // Rotated that way the silhouette half-width is baseRadius × scale × sin45,
  // so scale 2.194 would give 1.117 — algebraically flush with the skull.
  //
  // In practice it must be smaller than that. Rotated 45°, the roof's front
  // FACE sits half a base-radius nearer the camera than the skull's widest
  // line, and perspective inflates whatever is closer: matched on paper, it
  // overhangs on screen. 2.0 is the width that actually looks flush.
  const roofT = { pos: [0, 1.02, 0], rot: [0, 45, 0], scale: [2.0, 1.35, 2.0] };
  head.addChild(meshEntity(pc, app, pyramid, shell, { name: 'roof', ...roofT }));
  head.addChild(meshEntity(pc, app, pyramid, ridgeMat, {
    name: 'roofRidge', pos: roofT.pos, rot: roofT.rot,
    scale: [roofT.scale[0] * 1.045, roofT.scale[1] * 1.045, roofT.scale[2] * 1.045],
  }));

  // Visor — flattened against the front of the skull. In the reference it is
  // most of the face, not a pair of goggles, so it runs nearly skull-wide.
  // Its top lands at 0.30, just under the roof's 0.35 base plane.
  head.addChild(meshEntity(pc, app, sphere, visorMat, {
    name: 'visor', pos: [0, -0.45, 0.58], scale: [1.98, 1.5, 1.2],
  }));

  const eyes = [-0.45, 0.45].map((x, i) => {
    const e = meshEntity(pc, app, sphere, glowMat, {
      name: `eye${i}`, pos: [x, -0.24, 1.12], scale: [0.4, 0.31, 0.14],
    });
    head.addChild(e);
    return e;
  });

  // Ear pods with their lit rings. The ring sits a hair outboard of the pod
  // so it reads as a band around the rim rather than a stripe across it.
  const rings = [];
  for (const [i, x] of [-1.24, 1.24].entries()) {
    head.addChild(meshEntity(pc, app, cylinder, shellLow, {
      name: `ear${i}`, pos: [x, -0.3, 0], rot: [0, 0, 90], scale: [0.52, 0.34, 0.52],
    }));
    const ring = meshEntity(pc, app, torus, glowMat, {
      name: `earRing${i}`, pos: [x * 1.14, -0.3, 0], rot: [0, 0, 90], scale: [0.66, 0.66, 0.66],
    });
    head.addChild(ring);
    rings.push(ring);
  }

  // ── Neck and torso ────────────────────────────────────────────────────
  root.addChild(meshEntity(pc, app, cylinder, jointMat, {
    name: 'neck', pos: [0, 0.42, 0], scale: [0.5, 0.36, 0.5],
  }));

  root.addChild(meshEntity(pc, app, sphere, shell, {
    name: 'torso', pos: [0, -0.5, 0], scale: [1.86, 2.1, 1.5],
  }));

  // The house emblem on the chest: a small gable over a square.
  const emblem = new pc.Entity('emblem');
  emblem.setLocalPosition(0, -0.42, 0.74);
  root.addChild(emblem);
  emblem.addChild(meshEntity(pc, app, box, glowMat, {
    name: 'emblemBody', pos: [0, -0.1, 0], scale: [0.34, 0.3, 0.06],
  }));
  emblem.addChild(meshEntity(pc, app, pyramid, glowMat, {
    name: 'emblemRoof', pos: [0, 0.16, 0], rot: [0, 45, 0], scale: [0.42, 0.26, 0.42],
  }));

  // ── Arms ──────────────────────────────────────────────────────────────
  for (const [i, side] of [-1, 1].entries()) {
    root.addChild(meshEntity(pc, app, sphere, jointMat, {
      name: `shoulder${i}`, pos: [side * 0.98, -0.16, 0], scale: [0.5, 0.5, 0.5],
    }));
    root.addChild(meshEntity(pc, app, cylinder, shell, {
      name: `arm${i}`, pos: [side * 1.06, -0.66, 0.04], rot: [0, 0, side * 10], scale: [0.36, 0.86, 0.36],
    }));
    root.addChild(meshEntity(pc, app, sphere, jointMat, {
      name: `hand${i}`, pos: [side * 1.2, -1.12, 0.06], scale: [0.42, 0.42, 0.42],
    }));
  }

  return { root, head, eyes, rings, emblem };
}

/**
 * Lights. A cool key from the upper left matches the reference's lighting,
 * and the cyan fill from below right is what keeps the white shell from
 * going flat grey against a light page.
 */
export function buildLights(pc, root) {
  const key = new pc.Entity('key');
  key.addComponent('light', {
    type: 'directional', color: new pc.Color(1, 1, 1), intensity: 1.45, castShadows: false,
  });
  key.setLocalEulerAngles(38, 28, 0);
  root.addChild(key);

  const fill = new pc.Entity('fill');
  fill.addComponent('light', {
    type: 'directional', color: col(pc, PALETTE.glow), intensity: 0.7, castShadows: false,
  });
  fill.setLocalEulerAngles(-24, -142, 0);
  root.addChild(fill);

  return { key, fill };
}
