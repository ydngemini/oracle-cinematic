import { describe, expect, it } from 'vitest';
import {
  bearingTo,
  distanceTo,
  exitsFrom,
  groupByFloor,
  initialYaw,
  isWalkable,
  nextScene,
  sceneById,
  walkOrder,
  type PanoScene,
} from './panoGraph';

function scene(overrides: Partial<PanoScene> & { scene_id: string }): PanoScene {
  return {
    media_id: `m-${overrides.scene_id}`,
    url: `/api/media/${overrides.scene_id}`,
    floor_index: 0,
    label: '',
    position: null,
    heading_deg: null,
    neighbours: [],
    ...overrides,
  };
}

describe('walkability', () => {
  it('does not call a single 360 a walkthrough', () => {
    // Mirrors the server's tier-2 rule: one vantage point is a view. Claiming
    // "walk room-to-room" over it would be the badge lying.
    expect(isWalkable([scene({ scene_id: 'a' })])).toBe(false);
  });

  it('counts two scenes as walkable even with no recorded links', () => {
    // An ordered upload of 360s describes a route; adjacency is a refinement.
    expect(isWalkable([scene({ scene_id: 'a' }), scene({ scene_id: 'b' })])).toBe(true);
  });
});

describe('floors', () => {
  it('groups by storey and orders floors upward', () => {
    const scenes = [
      scene({ scene_id: 'up', floor_index: 1 }),
      scene({ scene_id: 'ground', floor_index: 0 }),
      scene({ scene_id: 'basement', floor_index: -1 }),
    ];
    expect([...groupByFloor(scenes).keys()]).toEqual([-1, 0, 1]);
  });

  it('keeps server order within a floor', () => {
    const scenes = [
      scene({ scene_id: 'hall', floor_index: 0 }),
      scene({ scene_id: 'kitchen', floor_index: 0 }),
    ];
    expect(groupByFloor(scenes).get(0)!.map((s) => s.scene_id)).toEqual(['hall', 'kitchen']);
  });

  it('treats a missing floor index as the ground floor', () => {
    const broken = { ...scene({ scene_id: 'x' }), floor_index: Number.NaN };
    expect([...groupByFloor([broken]).keys()]).toEqual([0]);
  });
});

describe('exits', () => {
  it('sorts positioned neighbours by bearing so hotspots read in view order', () => {
    const here = scene({
      scene_id: 'here',
      position: { x: 0, y: 0, z: 0 },
      neighbours: ['west', 'north', 'east'],
    });
    const scenes = [
      here,
      scene({ scene_id: 'north', position: { x: 0, y: 0, z: 10 } }),   // 0°
      scene({ scene_id: 'east', position: { x: 10, y: 0, z: 0 } }),    // 90°
      scene({ scene_id: 'west', position: { x: -10, y: 0, z: 0 } }),   // 270°
    ];
    expect(exitsFrom(scenes, 'here').map((s) => s.scene_id)).toEqual(['north', 'east', 'west']);
  });

  it('leaves unpositioned neighbours in listed order rather than guessing', () => {
    const here = scene({
      scene_id: 'here',
      position: { x: 0, y: 0, z: 0 },
      neighbours: ['unknown-1', 'east', 'unknown-2'],
    });
    const scenes = [
      here,
      scene({ scene_id: 'east', position: { x: 5, y: 0, z: 0 } }),
      scene({ scene_id: 'unknown-1' }),
      scene({ scene_id: 'unknown-2' }),
    ];
    // Positioned first (they can be placed), then the rest untouched.
    expect(exitsFrom(scenes, 'here').map((s) => s.scene_id))
      .toEqual(['east', 'unknown-1', 'unknown-2']);
  });

  it('keeps listed order entirely when the current scene has no position', () => {
    const scenes = [
      scene({ scene_id: 'here', neighbours: ['b', 'a'] }),
      scene({ scene_id: 'a', position: { x: 1, y: 0, z: 0 } }),
      scene({ scene_id: 'b', position: { x: 0, y: 0, z: 1 } }),
    ];
    expect(exitsFrom(scenes, 'here').map((s) => s.scene_id)).toEqual(['b', 'a']);
  });

  it('drops links to scenes that no longer exist', () => {
    const scenes = [scene({ scene_id: 'here', neighbours: ['deleted'] })];
    expect(exitsFrom(scenes, 'here')).toEqual([]);
  });

  it('returns nothing for an unknown scene', () => {
    expect(exitsFrom([scene({ scene_id: 'a' })], 'nope')).toEqual([]);
  });
});

describe('geometry', () => {
  const origin = scene({ scene_id: 'o', position: { x: 0, y: 0, z: 0 } });

  it('measures bearing clockwise from +z', () => {
    expect(bearingTo(origin, scene({ scene_id: 'n', position: { x: 0, y: 0, z: 5 } }))).toBe(0);
    expect(bearingTo(origin, scene({ scene_id: 'e', position: { x: 5, y: 0, z: 0 } }))).toBe(90);
    expect(bearingTo(origin, scene({ scene_id: 's', position: { x: 0, y: 0, z: -5 } }))).toBe(180);
    expect(bearingTo(origin, scene({ scene_id: 'w', position: { x: -5, y: 0, z: 0 } }))).toBe(270);
  });

  it('refuses a bearing when either end is unplaced', () => {
    expect(bearingTo(origin, scene({ scene_id: 'x' }))).toBeNull();
    expect(bearingTo(scene({ scene_id: 'y' }), origin)).toBeNull();
  });

  it('measures distance in three dimensions', () => {
    expect(distanceTo(origin, scene({ scene_id: 'd', position: { x: 3, y: 0, z: 4 } }))).toBe(5);
    expect(distanceTo(origin, scene({ scene_id: 'x' }))).toBeNull();
  });
});

describe('initial yaw', () => {
  it('converts a recorded heading to radians', () => {
    expect(initialYaw(scene({ scene_id: 'a', heading_deg: 180 }))).toBeCloseTo(Math.PI);
  });

  it('opens at zero when no heading was recorded', () => {
    // Predictable beats derived-and-arbitrary.
    expect(initialYaw(scene({ scene_id: 'a' }))).toBe(0);
    expect(initialYaw(null)).toBe(0);
  });
});

describe('walk order', () => {
  const scenes = [
    scene({ scene_id: 'g1', floor_index: 0 }),
    scene({ scene_id: 'g2', floor_index: 0 }),
    scene({ scene_id: 'u1', floor_index: 1 }),
  ];

  it('runs through each floor in turn', () => {
    expect(walkOrder(scenes).map((s) => s.scene_id)).toEqual(['g1', 'g2', 'u1']);
  });

  it('advances and wraps', () => {
    expect(nextScene(scenes, 'g1')!.scene_id).toBe('g2');
    expect(nextScene(scenes, 'u1')!.scene_id).toBe('g1');
  });

  it('starts at the beginning from an unknown scene', () => {
    expect(nextScene(scenes, null)!.scene_id).toBe('g1');
    expect(nextScene([], 'anything')).toBeNull();
  });
});

describe('lookup', () => {
  it('finds by id and tolerates nullish input', () => {
    const scenes = [scene({ scene_id: 'a' })];
    expect(sceneById(scenes, 'a')!.scene_id).toBe('a');
    expect(sceneById(scenes, null)).toBeNull();
    expect(sceneById(scenes, 'missing')).toBeNull();
  });
});
