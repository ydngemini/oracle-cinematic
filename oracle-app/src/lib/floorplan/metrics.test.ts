/**
 * Spatial metrics and the metric→imperial boundary.
 *
 * These numbers are what a rehab estimate is priced from, so the two ways they
 * go wrong quietly are both pinned here: interior walls being counted with one
 * face instead of two, and the unit conversion happening in more than one place.
 */

import { describe, expect, it } from 'vitest';

import { computeMetrics, diffMetrics, polygonArea, polygonPerimeter, toImperial, wallFaceArea } from './metrics';
import { EMPTY_FLOORPLAN, ZERO_METRICS } from './protocol';
import type { FloorplanDocument, FloorplanWall } from './protocol';

type Pt = [number, number];

const SQUARE: Pt[] = [
  [0, 0],
  [4, 0],
  [4, 3],
  [0, 3],
];

function doc(overrides: Partial<FloorplanDocument> = {}): FloorplanDocument {
  return { ...EMPTY_FLOORPLAN, ...overrides };
}

function wall(id: string, start: Pt, end: Pt, extra: Partial<FloorplanWall> = {}): FloorplanWall {
  return { id, start, end, thickness: 0.1, height: 2.5, levelId: null, interior: false, ...extra };
}

describe('polygon helpers', () => {
  it('computes area regardless of winding order', () => {
    expect(polygonArea(SQUARE)).toBe(12);
    expect(polygonArea([...SQUARE].reverse())).toBe(12);
  });

  it('treats the polygon as closed when measuring perimeter', () => {
    // 4 + 3 + 4 + 3 — the last vertex joins back to the first.
    expect(polygonPerimeter(SQUARE)).toBe(14);
  });

  it('returns zero for degenerate shapes', () => {
    expect(polygonArea([[0, 0], [1, 1]])).toBe(0);
    expect(polygonPerimeter([[0, 0]])).toBe(0);
  });
});

describe('wallFaceArea', () => {
  it('counts one finished face for an exterior wall', () => {
    expect(wallFaceArea(wall('w', [0, 0], [4, 0]))).toBe(10); // 4m × 2.5m
  });

  it('counts two for an interior wall', () => {
    // An interior wall separates two occupied spaces, so it takes twice the
    // drywall and paint. Getting this backwards halves the largest line item.
    expect(wallFaceArea(wall('w', [0, 0], [4, 0], { interior: true }))).toBe(20);
  });
});

describe('computeMetrics', () => {
  it('returns zeros for a null document rather than throwing', () => {
    expect(computeMetrics(null)).toEqual(ZERO_METRICS);
  });

  it('sums wall length and face area across mixed wall types', () => {
    const metrics = computeMetrics(
      doc({
        walls: [
          wall('a', [0, 0], [4, 0]),
          wall('b', [0, 0], [0, 3], { interior: true }),
        ],
      }),
    );

    expect(metrics.wall_linear_m).toBe(7);
    expect(metrics.wall_face_area_m2).toBe(10 + 15); // 4×2.5×1 + 3×2.5×2
    expect(metrics.counts.walls).toBe(2);
  });

  it('sums room area and perimeter, and tallies room types', () => {
    const metrics = computeMetrics(
      doc({
        rooms: [
          { id: 'r1', name: 'Bed 1', type: 'bedroom', polygon: SQUARE, levelId: null, boundaryWallIds: [] },
          { id: 'r2', name: 'Bed 2', type: 'bedroom', polygon: SQUARE, levelId: null, boundaryWallIds: [] },
          { id: 'r3', name: 'Bath', type: 'bathroom', polygon: SQUARE, levelId: null, boundaryWallIds: [] },
        ],
      }),
    );

    expect(metrics.floor_area_m2).toBe(36);
    expect(metrics.room_perimeter_m).toBe(42);
    expect(metrics.counts.by_room_type).toEqual({ bedroom: 2, bathroom: 1 });
  });

  it('separates doors from windows', () => {
    const metrics = computeMetrics(
      doc({
        openings: [
          { id: 'o1', kind: 'door', wallId: 'a', width: 0.9, height: 2 },
          { id: 'o2', kind: 'window', wallId: 'a', width: 1.2, height: 1.2 },
          { id: 'o3', kind: 'window', wallId: 'b', width: 1.2, height: 1.2 },
        ],
      }),
    );

    expect(metrics.counts.doors).toBe(1);
    expect(metrics.counts.windows).toBe(2);
  });

  it('rounds to the millimetre so dragging a vertex does not jitter the estimate', () => {
    const metrics = computeMetrics(doc({ walls: [wall('a', [0, 0], [1 / 3, 0])] }));

    expect(metrics.wall_linear_m).toBe(0.333);
  });
});

describe('toImperial', () => {
  it('converts lengths and areas at the costing boundary', () => {
    const imperial = toImperial({
      ...ZERO_METRICS,
      wall_linear_m: 10,
      wall_face_area_m2: 10,
      floor_area_m2: 100,
      room_perimeter_m: 10,
    });

    expect(imperial.wall_linear_ft).toBeCloseTo(32.808, 3);
    expect(imperial.floor_area_sqft).toBeCloseTo(1076.391, 3);
    expect(imperial.room_perimeter_ft).toBeCloseTo(32.808, 3);
  });

  it('passes counts through untouched — they are unitless', () => {
    const counts = { walls: 3, rooms: 2, doors: 1, windows: 4, by_room_type: { bedroom: 2 } };
    expect(toImperial({ ...ZERO_METRICS, counts }).counts).toEqual(counts);
  });
});

describe('diffMetrics', () => {
  it('reports signed deltas against the last-saved baseline', () => {
    const current = computeMetrics(doc({ walls: [wall('a', [0, 0], [4, 0])] }));
    const baseline = computeMetrics(doc({ walls: [wall('a', [0, 0], [10, 0])] }));

    expect(diffMetrics(current, baseline).wall_linear_m).toBe(-6);
  });

  it('covers room types present on only one side', () => {
    const current = computeMetrics(
      doc({ rooms: [{ id: 'r', name: 'Bath', type: 'bathroom', polygon: SQUARE, levelId: null, boundaryWallIds: [] }] }),
    );

    const delta = diffMetrics(current, ZERO_METRICS);
    expect(delta.counts.by_room_type.bathroom).toBe(1);
    expect(diffMetrics(ZERO_METRICS, current).counts.by_room_type.bathroom).toBe(-1);
  });
});
