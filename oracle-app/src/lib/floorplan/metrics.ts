/**
 * Derive SpatialMetrics from a FloorplanDocument.
 *
 * These numbers used to arrive over the wire from the Pascal guest. Now that the
 * editor is ours, we compute them here — which also makes them testable without
 * a browser, an iframe, or a third-party editor being reachable.
 *
 * Everything is metres in, metres out. useRehabCalculator does the imperial
 * conversion at the point of costing, so there is exactly one place where a unit
 * mistake can happen.
 */

import type {
  FloorplanDocument,
  FloorplanRoom,
  FloorplanWall,
  RoomType,
  SpatialMetrics,
} from './protocol';
import { ZERO_METRICS } from './protocol';

const M_TO_FT = 3.280839895013123;
const M2_TO_SQFT = 10.763910416709722;

/**
 * The same metrics in the units the cost table is priced in.
 *
 * US rehab pricing is quoted per linear foot and per square foot, so the
 * conversion happens exactly once, here, at the boundary between the geometry
 * (always metric) and the money (always imperial). Converting anywhere else is
 * how a plan silently comes out 3.28× too expensive.
 */
export interface ImperialMetrics {
  wall_linear_ft: number;
  wall_face_area_sqft: number;
  floor_area_sqft: number;
  room_perimeter_ft: number;
  counts: SpatialMetrics['counts'];
}

export function toImperial(metrics: SpatialMetrics): ImperialMetrics {
  return {
    wall_linear_ft: metrics.wall_linear_m * M_TO_FT,
    wall_face_area_sqft: metrics.wall_face_area_m2 * M2_TO_SQFT,
    floor_area_sqft: metrics.floor_area_m2 * M2_TO_SQFT,
    room_perimeter_ft: metrics.room_perimeter_m * M_TO_FT,
    counts: metrics.counts,
  };
}

/** Signed change between two metric snapshots — drives the "since last save" badge. */
export function diffMetrics(current: SpatialMetrics, baseline: SpatialMetrics): SpatialMetrics {
  const types = new Set<RoomType>([
    ...(Object.keys(current.counts.by_room_type) as RoomType[]),
    ...(Object.keys(baseline.counts.by_room_type) as RoomType[]),
  ]);
  const byRoomType: Partial<Record<RoomType, number>> = {};
  for (const type of types) {
    byRoomType[type] =
      (current.counts.by_room_type[type] ?? 0) - (baseline.counts.by_room_type[type] ?? 0);
  }

  return {
    wall_linear_m: round(current.wall_linear_m - baseline.wall_linear_m),
    wall_face_area_m2: round(current.wall_face_area_m2 - baseline.wall_face_area_m2),
    floor_area_m2: round(current.floor_area_m2 - baseline.floor_area_m2),
    room_perimeter_m: round(current.room_perimeter_m - baseline.room_perimeter_m),
    counts: {
      walls: current.counts.walls - baseline.counts.walls,
      rooms: current.counts.rooms - baseline.counts.rooms,
      doors: current.counts.doors - baseline.counts.doors,
      windows: current.counts.windows - baseline.counts.windows,
      by_room_type: byRoomType,
    },
  };
}

export function wallLength(wall: Pick<FloorplanWall, 'start' | 'end'>): number {
  return Math.hypot(wall.end[0] - wall.start[0], wall.end[1] - wall.start[1]);
}

/** Shoelace area in m². Sign is discarded, so winding order does not matter. */
export function polygonArea(polygon: ReadonlyArray<readonly [number, number]>): number {
  if (polygon.length < 3) return 0;
  let twice = 0;
  for (let i = 0; i < polygon.length; i += 1) {
    const [x1, y1] = polygon[(i - 1 + polygon.length) % polygon.length];
    const [x2, y2] = polygon[i];
    twice += x1 * y2 - x2 * y1;
  }
  return Math.abs(twice) / 2;
}

/** Closed perimeter in metres — the last vertex joins back to the first. */
export function polygonPerimeter(polygon: ReadonlyArray<readonly [number, number]>): number {
  if (polygon.length < 2) return 0;
  let total = 0;
  for (let i = 0; i < polygon.length; i += 1) {
    const [x1, y1] = polygon[(i - 1 + polygon.length) % polygon.length];
    const [x2, y2] = polygon[i];
    total += Math.hypot(x2 - x1, y2 - y1);
  }
  return total;
}

export function roomArea(room: Pick<FloorplanRoom, 'polygon'>): number {
  return polygonArea(room.polygon);
}

/**
 * Paintable/board face area for one wall.
 *
 * An interior wall separates two occupied spaces, so it has two finished faces
 * and takes twice the drywall and paint. An exterior wall only presents one face
 * to the inside. Getting this backwards silently halves or doubles the largest
 * line item in a rehab estimate, which is why it is stated here once rather than
 * inferred at each call site.
 */
export function wallFaceArea(wall: FloorplanWall): number {
  return wallLength(wall) * wall.height * (wall.interior ? 2 : 1);
}

export function computeMetrics(document: FloorplanDocument | null | undefined): SpatialMetrics {
  if (!document) return ZERO_METRICS;

  const walls = document.walls ?? [];
  const rooms = document.rooms ?? [];
  const openings = document.openings ?? [];

  let wallLinear = 0;
  let wallFace = 0;
  for (const wall of walls) {
    wallLinear += wallLength(wall);
    wallFace += wallFaceArea(wall);
  }

  let floorArea = 0;
  let roomPerimeter = 0;
  const byRoomType: Partial<Record<RoomType, number>> = {};
  for (const room of rooms) {
    floorArea += polygonArea(room.polygon);
    roomPerimeter += polygonPerimeter(room.polygon);
    byRoomType[room.type] = (byRoomType[room.type] ?? 0) + 1;
  }

  let doors = 0;
  let windows = 0;
  for (const opening of openings) {
    if (opening.kind === 'door') doors += 1;
    else if (opening.kind === 'window') windows += 1;
  }

  return {
    wall_linear_m: round(wallLinear),
    wall_face_area_m2: round(wallFace),
    floor_area_m2: round(floorArea),
    room_perimeter_m: round(roomPerimeter),
    counts: {
      walls: walls.length,
      rooms: rooms.length,
      doors,
      windows,
      by_room_type: byRoomType,
    },
  };
}

/**
 * Three decimals — a millimetre.
 *
 * Without this, dragging a vertex produces float noise in the 12th decimal that
 * makes every metric look changed, which in turn makes the "unsaved changes"
 * badge flicker and the rehab delta jitter by fractions of a cent.
 */
function round(value: number): number {
  return Math.round(value * 1000) / 1000;
}
