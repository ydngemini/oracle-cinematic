/**
 * In-house floor-plan editor state.
 *
 * Replaces useFloorplanBridge. That hook spoke a postMessage protocol to a
 * Pascal guest — but Pascal's embed is a read-only viewer with, in their words,
 * "no oEmbed endpoint, JavaScript API, or postMessage interface", so the guest
 * half could never exist without forking and hosting their Next.js app. Owning
 * the editor removes the dependency entirely and makes the whole path testable.
 *
 * The returned surface intentionally matches what RehabEditorDrawer already
 * destructured, so swapping the two is a small change at the call site.
 *
 * Units are metres. Coordinates are [x, z] on the level plane, matching
 * protocol.ts and backend/floorplan_pipeline/schema.py.
 */

import { useCallback, useMemo, useRef, useState } from 'react';

import type {
  FloorplanDocument,
  FloorplanRoom,
  FloorplanWall,
  RoomType,
  SpatialMetrics,
} from './protocol';
import { EMPTY_FLOORPLAN } from './protocol';
import { computeMetrics } from './metrics';

export type Point = [number, number];

export interface UseFloorplanEditorOptions {
  /** Persisted plan, or null while it is still loading. */
  initialDocument?: FloorplanDocument | null;
  readOnly?: boolean;
}

export interface UseFloorplanEditorResult {
  document: FloorplanDocument;
  metrics: SpatialMetrics;
  /** Metrics as at the last save — the rehab delta is measured against these. */
  baselineMetrics: SpatialMetrics;
  dirty: boolean;
  selectedId: string | null;
  select: (id: string | null) => void;

  addWall: (start: Point, end: Point, options?: Partial<FloorplanWall>) => string | null;
  moveWall: (id: string, start: Point, end: Point) => void;
  setWallInterior: (id: string, interior: boolean) => void;
  addRoom: (polygon: Point[], type?: RoomType, name?: string) => string | null;
  renameRoom: (id: string, name: string, type: RoomType) => void;
  remove: (id: string) => void;
  clear: () => void;

  /** Replace the whole document (e.g. output of the parcel/vision pipeline). */
  load: (document: FloorplanDocument) => void;
  /** Current document, for persisting. Async to match the old bridge contract. */
  requestDocument: () => Promise<FloorplanDocument>;
  /** Re-baseline after a successful save. */
  markSaved: (document: FloorplanDocument) => void;
  undo: () => void;
  canUndo: boolean;
}

const MIN_WALL_M = 0.05;

function newId(prefix: string): string {
  const globalCrypto = typeof crypto !== 'undefined' ? crypto : undefined;
  if (globalCrypto && typeof globalCrypto.randomUUID === 'function') {
    return `${prefix}_${globalCrypto.randomUUID().slice(0, 8)}`;
  }
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Anything a human edits is manual, and manual output must never claim to be
 * machine-generated — the AI-media disclosure hangs off this flag, and the
 * database CHECK enforces it for the pipeline's side.
 */
function asManual(document: FloorplanDocument): FloorplanDocument {
  const previous = document.provenance;
  if (previous?.source === 'manual' && previous.ai_generated === false) return document;
  return {
    ...document,
    provenance: {
      ...previous,
      source: 'manual',
      ai_generated: false,
      // Keep the machine's note so the history of the plan is not erased —
      // an agent editing a vision-derived plan should still see where it began.
      notes: previous?.notes,
    },
  };
}

export function useFloorplanEditor({
  initialDocument = null,
  readOnly = false,
}: UseFloorplanEditorOptions = {}): UseFloorplanEditorResult {
  const [document, setDocument] = useState<FloorplanDocument>(
    () => initialDocument ?? EMPTY_FLOORPLAN,
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const history = useRef<FloorplanDocument[]>([]);

  // The document the metrics were baselined against. Held in state (not a ref)
  // so the rehab delta recomputes when it changes.
  const [savedDocument, setSavedDocument] = useState<FloorplanDocument>(
    () => initialDocument ?? EMPTY_FLOORPLAN,
  );

  // `loadedKey` lets a late-arriving fetch replace the starting document without
  // clobbering edits the agent has already made while it was in flight.
  const loadedRef = useRef(false);
  if (!loadedRef.current && initialDocument && !dirty) {
    loadedRef.current = true;
    if (document !== initialDocument) {
      setDocument(initialDocument);
      setSavedDocument(initialDocument);
    }
  }

  const mutate = useCallback(
    (fn: (current: FloorplanDocument) => FloorplanDocument) => {
      if (readOnly) return;
      setDocument((current) => {
        history.current = [...history.current.slice(-49), current];
        return asManual(fn(current));
      });
      setDirty(true);
    },
    [readOnly],
  );

  const addWall = useCallback(
    (start: Point, end: Point, options: Partial<FloorplanWall> = {}) => {
      // readOnly returns null rather than an id: handing back an id for a wall
      // that was never added invites the caller to select or delete a ghost.
      // A zero-length wall is almost always a stray click, and it would add a
      // node the user cannot see or select to delete.
      if (readOnly) return null;
      if (Math.hypot(end[0] - start[0], end[1] - start[1]) < MIN_WALL_M) return null;
      const id = newId('wall');
      mutate((current) => ({
        ...current,
        walls: [
          ...current.walls,
          {
            id,
            start,
            end,
            thickness: options.thickness ?? 0.1,
            height: options.height ?? 2.5,
            levelId: options.levelId ?? null,
            interior: options.interior ?? false,
          },
        ],
      }));
      return id;
    },
    [mutate, readOnly],
  );

  const moveWall = useCallback(
    (id: string, start: Point, end: Point) => {
      mutate((current) => ({
        ...current,
        walls: current.walls.map((wall) => (wall.id === id ? { ...wall, start, end } : wall)),
      }));
    },
    [mutate],
  );

  const setWallInterior = useCallback(
    (id: string, interior: boolean) => {
      mutate((current) => ({
        ...current,
        walls: current.walls.map((wall) => (wall.id === id ? { ...wall, interior } : wall)),
      }));
    },
    [mutate],
  );

  const addRoom = useCallback(
    (polygon: Point[], type: RoomType = 'other', name?: string) => {
      // Fewer than three vertices encloses no area, so it would contribute
      // nothing to square footage while still appearing in the room count.
      if (readOnly) return null;
      if (polygon.length < 3) return null;
      const id = newId('room');
      mutate((current) => ({
        ...current,
        rooms: [
          ...current.rooms,
          {
            id,
            name: name || `${type.charAt(0).toUpperCase()}${type.slice(1)}`,
            type,
            polygon,
            levelId: null,
            boundaryWallIds: [],
          } as FloorplanRoom,
        ],
      }));
      return id;
    },
    [mutate, readOnly],
  );

  const renameRoom = useCallback(
    (id: string, name: string, type: RoomType) => {
      mutate((current) => ({
        ...current,
        rooms: current.rooms.map((room) => (room.id === id ? { ...room, name, type } : room)),
      }));
    },
    [mutate],
  );

  const remove = useCallback(
    (id: string) => {
      mutate((current) => ({
        ...current,
        walls: current.walls.filter((wall) => wall.id !== id),
        rooms: current.rooms.filter((room) => room.id !== id),
        // Openings hang off a wall; deleting the wall must not leave a door
        // floating in space with a dangling wallId.
        openings: current.openings.filter((opening) => opening.wallId !== id),
      }));
      setSelectedId((current) => (current === id ? null : current));
    },
    [mutate],
  );

  const clear = useCallback(() => {
    mutate((current) => ({ ...current, walls: [], rooms: [], openings: [] }));
    setSelectedId(null);
  }, [mutate]);

  const load = useCallback((next: FloorplanDocument) => {
    history.current = [];
    setDocument(next);
    setSavedDocument(next);
    setSelectedId(null);
    setDirty(false);
    loadedRef.current = true;
  }, []);

  const undo = useCallback(() => {
    setDocument((current) => {
      const previous = history.current.pop();
      return previous ?? current;
    });
  }, []);

  const requestDocument = useCallback(async () => document, [document]);

  const markSaved = useCallback((saved: FloorplanDocument) => {
    setSavedDocument(saved);
    setDirty(false);
  }, []);

  const metrics = useMemo(() => computeMetrics(document), [document]);
  const baselineMetrics = useMemo(() => computeMetrics(savedDocument), [savedDocument]);

  return {
    document,
    metrics,
    baselineMetrics,
    dirty,
    selectedId,
    select: setSelectedId,
    addWall,
    moveWall,
    setWallInterior,
    addRoom,
    renameRoom,
    remove,
    clear,
    load,
    requestDocument,
    markSaved,
    undo,
    canUndo: history.current.length > 0,
  };
}
