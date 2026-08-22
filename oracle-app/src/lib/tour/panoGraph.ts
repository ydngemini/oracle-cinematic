/**
 * Scene-graph maths for 360° walkthroughs.
 *
 * Kept out of the viewer so it can be tested without a canvas — the WebGL half
 * needs a GPU context, this half is arithmetic. The viewer imports these and
 * owns nothing but rendering and input.
 *
 * The shape here mirrors `GET /api/crm/property-tour`'s `pano_scenes`, which is
 * deliberately permissive about what is known: a scene may have no recorded
 * position and no explicit neighbours. Everything below degrades to capture
 * order in that case rather than inventing a spatial relationship.
 */

export interface PanoScene {
  scene_id: string;
  media_id: string;
  url: string;
  floor_index: number;
  label: string;
  /** Metres from the plan origin. Null when nobody surveyed it. */
  position: { x: number; y: number; z: number } | null;
  /** Compass bearing to open facing, in degrees. Null when unrecorded. */
  heading_deg: number | null;
  /** Scene ids reachable from here. */
  neighbours: string[];
}

/** Scenes grouped by storey, each group in walk order. */
export function groupByFloor(scenes: PanoScene[]): Map<number, PanoScene[]> {
  const floors = new Map<number, PanoScene[]>();
  for (const scene of scenes) {
    const index = Number.isFinite(scene.floor_index) ? scene.floor_index : 0;
    const group = floors.get(index);
    if (group) group.push(scene);
    else floors.set(index, [scene]);
  }
  return new Map([...floors.entries()].sort((a, b) => a[0] - b[0]));
}

export function sceneById(scenes: PanoScene[], id: string | null | undefined): PanoScene | null {
  if (!id) return null;
  return scenes.find((scene) => scene.scene_id === id) ?? null;
}

/**
 * Where to go from `from`, in the order the viewer should offer them.
 *
 * Neighbours with a known position are sorted by bearing so the hotspots read
 * left-to-right the way they appear in the sphere. Ones without a position keep
 * their listed order — sorting them by a bearing we do not have would be
 * arbitrary, and arbitrary is indistinguishable from wrong to whoever is
 * walking through the house.
 */
export function exitsFrom(scenes: PanoScene[], fromId: string): PanoScene[] {
  const from = sceneById(scenes, fromId);
  if (!from) return [];

  const targets = from.neighbours
    .map((id) => sceneById(scenes, id))
    .filter((scene): scene is PanoScene => scene !== null);

  if (!from.position) return targets;

  const placed = targets.filter((scene) => scene.position !== null);
  const unplaced = targets.filter((scene) => scene.position === null);
  placed.sort((a, b) => bearingBetween(from, a) - bearingBetween(from, b));
  return [...placed, ...unplaced];
}

/**
 * Compass bearing from one scene to another, 0–360°, 0 = +z ("north").
 *
 * Returns null unless both ends are positioned — a hotspot cannot be placed on
 * the sphere from a guess, and the viewer renders those as a plain list instead.
 */
export function bearingTo(from: PanoScene, to: PanoScene): number | null {
  if (!from.position || !to.position) return null;
  return bearingBetween(from, to);
}

function bearingBetween(from: PanoScene, to: PanoScene): number {
  const dx = (to.position!.x) - (from.position!.x);
  const dz = (to.position!.z) - (from.position!.z);
  const degrees = (Math.atan2(dx, dz) * 180) / Math.PI;
  return (degrees + 360) % 360;
}

/** Straight-line metres between two positioned scenes, else null. */
export function distanceTo(from: PanoScene, to: PanoScene): number | null {
  if (!from.position || !to.position) return null;
  const dx = to.position.x - from.position.x;
  const dy = to.position.y - from.position.y;
  const dz = to.position.z - from.position.z;
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

/**
 * Yaw, in radians, to open a scene at.
 *
 * `heading_deg` is a compass bearing; the renderer's yaw is the rotation of the
 * sphere about y. Unrecorded headings open at 0 rather than at some derived
 * angle — an arbitrary starting direction is not an improvement on a consistent
 * one, and a consistent one is at least predictable between scenes.
 */
export function initialYaw(scene: PanoScene | null): number {
  if (!scene || scene.heading_deg == null || !Number.isFinite(scene.heading_deg)) return 0;
  return (scene.heading_deg * Math.PI) / 180;
}

/**
 * Whether these scenes constitute a walkthrough rather than a single view.
 *
 * Mirrors the server's rule so the UI cannot disagree with the tier badge. Two
 * scenes with no link between them still count: an agent who uploads a set of
 * 360s in order has described a route, even without recording adjacency.
 */
export function isWalkable(scenes: PanoScene[]): boolean {
  return scenes.length >= 2;
}

/**
 * A stable walk order for keyboard navigation and the "next room" control.
 *
 * Floors ascend, and scenes keep server order within a floor — that is capture
 * order, which is the closest thing to an intended route that exists when no
 * positions were recorded.
 */
export function walkOrder(scenes: PanoScene[]): PanoScene[] {
  return [...groupByFloor(scenes).values()].flat();
}

/** The next scene in walk order, wrapping at the end. Null for an empty tour. */
export function nextScene(scenes: PanoScene[], currentId: string | null): PanoScene | null {
  const order = walkOrder(scenes);
  if (order.length === 0) return null;
  const index = order.findIndex((scene) => scene.scene_id === currentId);
  return order[(index + 1) % order.length] ?? order[0];
}
