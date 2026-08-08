/**
 * @vitest-environment jsdom
 *
 * Editor state. The behaviours worth pinning are the ones that would corrupt an
 * estimate or a disclosure rather than merely look wrong: provenance stamping,
 * degenerate geometry, and orphaned openings.
 */

import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useFloorplanEditor } from './useFloorplanEditor';
import { EMPTY_FLOORPLAN, type FloorplanDocument } from './protocol';

const AI_DOC: FloorplanDocument = {
  ...EMPTY_FLOORPLAN,
  provenance: {
    source: 'ai_vision',
    ai_generated: true,
    model_version: 'opencv-1',
    notes: 'Derived from listing photo',
  },
};

describe('useFloorplanEditor', () => {
  it('starts from the persisted document', () => {
    const { result } = renderHook(() =>
      useFloorplanEditor({ initialDocument: { ...EMPTY_FLOORPLAN, walls: [] } }),
    );

    expect(result.current.document.walls).toEqual([]);
    expect(result.current.dirty).toBe(false);
  });

  it('adds a wall and recomputes metrics', () => {
    const { result } = renderHook(() => useFloorplanEditor());

    act(() => { result.current.addWall([0, 0], [4, 0]); });

    expect(result.current.document.walls).toHaveLength(1);
    expect(result.current.metrics.wall_linear_m).toBe(4);
    expect(result.current.dirty).toBe(true);
  });

  it('refuses a zero-length wall', () => {
    // Almost always a stray click, and it would add a node too small to see or
    // select in order to delete.
    const { result } = renderHook(() => useFloorplanEditor());

    let id: string | null = 'unset';
    act(() => { id = result.current.addWall([2, 2], [2, 2]); });

    expect(id).toBeNull();
    expect(result.current.document.walls).toHaveLength(0);
  });

  it('refuses a polygon that encloses no area', () => {
    const { result } = renderHook(() => useFloorplanEditor());

    let id: string | null = 'unset';
    act(() => { id = result.current.addRoom([[0, 0], [1, 0]]); });

    expect(id).toBeNull();
    expect(result.current.document.rooms).toHaveLength(0);
  });

  it('adds a room and reports its area', () => {
    const { result } = renderHook(() => useFloorplanEditor());

    act(() => { result.current.addRoom([[0, 0], [4, 0], [4, 3], [0, 3]], 'bedroom'); });

    expect(result.current.metrics.floor_area_m2).toBe(12);
    expect(result.current.metrics.counts.by_room_type).toEqual({ bedroom: 1 });
  });

  describe('provenance', () => {
    it('stamps a human edit as manual', () => {
      // A plan a person drew must never claim to be machine output, and vice
      // versa — the AI-media disclosure hangs off this flag.
      const { result } = renderHook(() => useFloorplanEditor({ initialDocument: AI_DOC }));

      act(() => { result.current.addWall([0, 0], [4, 0]); });

      expect(result.current.document.provenance.source).toBe('manual');
      expect(result.current.document.provenance.ai_generated).toBe(false);
    });

    it('keeps the machine note so the plan’s origin is not erased', () => {
      const { result } = renderHook(() => useFloorplanEditor({ initialDocument: AI_DOC }));

      act(() => { result.current.addWall([0, 0], [4, 0]); });

      expect(result.current.document.provenance.notes).toBe('Derived from listing photo');
    });
  });

  it('removes openings attached to a deleted wall', () => {
    // Otherwise a door survives with a wallId pointing at nothing, and it keeps
    // counting toward the door line item.
    const { result } = renderHook(() =>
      useFloorplanEditor({
        initialDocument: {
          ...EMPTY_FLOORPLAN,
          walls: [{ id: 'w1', start: [0, 0], end: [4, 0], thickness: 0.1, height: 2.5, levelId: null, interior: false }],
          openings: [{ id: 'o1', kind: 'door', wallId: 'w1', width: 0.9, height: 2 }],
        },
      }),
    );

    act(() => { result.current.remove('w1'); });

    expect(result.current.document.walls).toHaveLength(0);
    expect(result.current.document.openings).toHaveLength(0);
    expect(result.current.metrics.counts.doors).toBe(0);
  });

  it('clears the selection when the selected item is removed', () => {
    const { result } = renderHook(() => useFloorplanEditor());

    let id: string | null = null;
    act(() => { id = result.current.addWall([0, 0], [4, 0]); });
    act(() => { result.current.select(id); });
    act(() => { result.current.remove(id as unknown as string); });

    expect(result.current.selectedId).toBeNull();
  });

  it('measures the rehab delta against the last save, not the session start', () => {
    const { result } = renderHook(() => useFloorplanEditor());

    act(() => { result.current.addWall([0, 0], [4, 0]); });
    act(() => { result.current.markSaved(result.current.document); });

    expect(result.current.dirty).toBe(false);
    expect(result.current.baselineMetrics.wall_linear_m).toBe(4);

    act(() => { result.current.addWall([0, 0], [0, 3]); });

    expect(result.current.metrics.wall_linear_m).toBe(7);
    expect(result.current.baselineMetrics.wall_linear_m).toBe(4);
  });

  it('undoes the last edit', () => {
    const { result } = renderHook(() => useFloorplanEditor());

    act(() => { result.current.addWall([0, 0], [4, 0]); });
    act(() => { result.current.addWall([0, 0], [0, 3]); });
    act(() => { result.current.undo(); });

    expect(result.current.document.walls).toHaveLength(1);
  });

  it('load() replaces the document and resets the dirty flag', () => {
    // This is the path the parcel/vision pipeline output arrives on.
    const { result } = renderHook(() => useFloorplanEditor());

    act(() => { result.current.addWall([0, 0], [4, 0]); });
    act(() => { result.current.load(AI_DOC); });

    expect(result.current.document.provenance.source).toBe('ai_vision');
    expect(result.current.dirty).toBe(false);
  });

  it('ignores edits in read-only mode', () => {
    const { result } = renderHook(() => useFloorplanEditor({ readOnly: true }));

    let wallId: string | null = 'unset';
    let roomId: string | null = 'unset';
    act(() => {
      wallId = result.current.addWall([0, 0], [4, 0]);
      roomId = result.current.addRoom([[0, 0], [4, 0], [4, 3]]);
    });

    expect(result.current.document.walls).toHaveLength(0);
    expect(result.current.document.rooms).toHaveLength(0);
    expect(result.current.dirty).toBe(false);
    // Returning an id for something never added invites selecting a ghost.
    expect(wallId).toBeNull();
    expect(roomId).toBeNull();
  });
});
