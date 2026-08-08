/**
 * Typed postMessage contract between Oracle (host) and the Pascal 3D building
 * editor (guest, running in its own bundle inside an iframe).
 *
 * Why an iframe: Pascal is React Three Fiber + Three.js + WebGPU and its npm
 * package ships raw .tsx with a `next` peer dependency. Oracle is Vite, and
 * CLAUDE.md forbids Three.js in this bundle. Isolating Pascal behind an origin
 * boundary keeps three/R3F/drei/Radix/Tailwind/next out of Oracle entirely and
 * gives us a hard crash boundary around a WebGPU canvas.
 *
 * This file is the ONLY shared vocabulary between the two apps. Copy it into
 * the Pascal host app verbatim (or publish it as a tiny shared package) so both
 * sides are compiled against the same message union.
 *
 * UNITS: Pascal's scene graph is metric — WallNode.start/end are [x, z] metres,
 * thickness/height are metres, ZoneNode.polygon is [x, z] metres. Everything
 * that crosses this boundary stays METRIC. Conversion to imperial happens once,
 * in metrics.ts, at the point of rehab costing. Do not convert twice.
 */

export const FLOORPLAN_PROTOCOL_VERSION = 1;


// ---------------------------------------------------------------------------
// Persisted schema — this is what lands in Postgres (jsonb) and what the CV
// pipeline in backend/floorplan_pipeline emits. Deliberately a small, flat,
// engine-agnostic subset of Pascal's scene graph: enough to re-hydrate the
// editor, drive rehab costing, and be read by a non-Three.js renderer later.
// ---------------------------------------------------------------------------

export interface FloorplanWall {
  id: string;
  /** [x, z] in metres, on the level plane. */
  start: [number, number];
  end: [number, number];
  /** Metres. Pascal default 0.1. */
  thickness: number;
  /** Metres. Pascal default 2.5. */
  height: number;
  /** Pascal level node id this wall belongs to. */
  levelId: string | null;
  /** true when the wall separates two interior zones (drives drywall qty ×2). */
  interior: boolean;
}

export interface FloorplanRoom {
  id: string;
  name: string;
  /** Semantic type used by the rehab cost table. */
  type: RoomType;
  /** Closed polygon of [x, z] metres. Not guaranteed convex. */
  polygon: Array<[number, number]>;
  levelId: string | null;
  boundaryWallIds: string[];
}

export type RoomType =
  | 'bedroom'
  | 'bathroom'
  | 'kitchen'
  | 'living'
  | 'dining'
  | 'hallway'
  | 'garage'
  | 'utility'
  | 'closet'
  | 'other';

export const ROOM_TYPES: readonly RoomType[] = [
  'bedroom', 'bathroom', 'kitchen', 'living', 'dining',
  'hallway', 'garage', 'utility', 'closet', 'other',
] as const;

export interface FloorplanOpening {
  id: string;
  kind: 'door' | 'window';
  wallId: string | null;
  /** Metres. */
  width: number;
  height: number;
}

export interface FloorplanLevel {
  id: string;
  name: string;
  /** Storey index; 0 = ground. Negative = basement. */
  index: number;
}

/**
 * The full persisted document. `schema_version` is checked on read so an old
 * row never silently mis-renders after the shape changes.
 */
export interface FloorplanDocument {
  schema_version: number;
  units: 'metric';
  levels: FloorplanLevel[];
  walls: FloorplanWall[];
  rooms: FloorplanRoom[];
  openings: FloorplanOpening[];
  /**
   * Provenance. `ai_generated` MUST be true for anything the CV pipeline
   * produced, so the UI can carry the same AI-media disclosure the splat
   * viewer does. Never default this to false for machine output.
   */
  provenance: {
    source: 'manual' | 'ai_vision' | 'parcel_vector' | 'imported';
    ai_generated: boolean;
    model_version?: string;
    confidence?: number;
    notes?: string;
  };
}

export const EMPTY_FLOORPLAN: FloorplanDocument = {
  schema_version: FLOORPLAN_PROTOCOL_VERSION,
  units: 'metric',
  levels: [],
  walls: [],
  rooms: [],
  openings: [],
  provenance: { source: 'manual', ai_generated: false },
};

// ---------------------------------------------------------------------------
// Derived spatial metrics — computed guest-side on every scene mutation and
// sent to the host. The host never needs the raw scene graph to cost a rehab.
// ---------------------------------------------------------------------------

export interface SpatialMetrics {
  /** Metres. Sum of wall centreline lengths. */
  wall_linear_m: number;
  /** Square metres. Sum of wall face area (both faces of interior walls). */
  wall_face_area_m2: number;
  /** Square metres. Sum of room polygon areas. */
  floor_area_m2: number;
  /** Metres. Sum of room polygon perimeters — drives trim/baseboard. */
  room_perimeter_m: number;
  counts: {
    walls: number;
    rooms: number;
    doors: number;
    windows: number;
    /** Per-RoomType tallies; drives fixture-count line items. */
    by_room_type: Partial<Record<RoomType, number>>;
  };
}

export const ZERO_METRICS: SpatialMetrics = {
  wall_linear_m: 0,
  wall_face_area_m2: 0,
  floor_area_m2: 0,
  room_perimeter_m: 0,
  counts: { walls: 0, rooms: 0, doors: 0, windows: 0, by_room_type: {} },
};
