/**
 * 2D floor-plan canvas — draw walls and rooms, in metres.
 *
 * SVG rather than WebGL on purpose: a floor plan is a few hundred line segments,
 * the browser already composites vectors well, and it keeps this free of the
 * Three.js/WebGPU dependency the abandoned Pascal route would have dragged in.
 * Oracle's convention is raw rendering with no UI libraries, and this holds to it.
 *
 * World units are metres, [x, z], matching protocol.ts and the Python schema.
 * The view transform is a single scale + offset so screen↔world stays one
 * reversible pair of helpers instead of matrices scattered through handlers.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { MousePointer2, Minus, Square, Trash2, Undo2 } from 'lucide-react';
import { ROOM_TYPES } from '../lib/floorplan/protocol';
import { polygonArea, wallLength } from '../lib/floorplan/metrics';
import styles from './FloorplanCanvas.module.css';

const GRID_M = 0.5;          // snap step — 500 mm reads as deliberate on plan
const MIN_SCALE = 8;         // px per metre
const MAX_SCALE = 160;
const HIT_TOLERANCE_PX = 8;

const TOOLS = [
  { key: 'select', label: 'Select', Icon: MousePointer2 },
  { key: 'wall', label: 'Wall', Icon: Minus },
  { key: 'room', label: 'Room', Icon: Square },
];

function snap(value) {
  return Math.round(value / GRID_M) * GRID_M;
}

/** Perpendicular distance from a point to a segment, in world units. */
function distanceToSegment([px, py], [ax, ay], [bx, by]) {
  const dx = bx - ax;
  const dy = by - ay;
  const lengthSq = dx * dx + dy * dy;
  if (lengthSq === 0) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / lengthSq;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

function pointInPolygon([x, y], polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    const intersects = yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi;
    if (intersects) inside = !inside;
  }
  return inside;
}

export function FloorplanCanvas({
  document: doc,
  selectedId,
  onSelect,
  onAddWall,
  onAddRoom,
  onRemove,
  onUndo,
  readOnly = false,
}) {
  const svgRef = useRef(null);
  const [tool, setTool] = useState('select');
  const [roomType, setRoomType] = useState('bedroom');
  const [view, setView] = useState({ scale: 40, x: 0, z: 0 });
  const [draft, setDraft] = useState(null);   // in-progress wall or room
  const [cursor, setCursor] = useState(null);
  const [size, setSize] = useState({ width: 800, height: 520 });

  // Track the rendered box so world↔screen stays correct through layout changes
  // (the drawer animates open, so an initial measurement alone would be wrong).
  useEffect(() => {
    const element = svgRef.current;
    if (!element || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setSize({ width, height });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const toScreen = useCallback(
    ([x, z]) => [size.width / 2 + (x - view.x) * view.scale, size.height / 2 + (z - view.z) * view.scale],
    [size, view],
  );

  const toWorld = useCallback(
    (clientX, clientY) => {
      const rect = svgRef.current?.getBoundingClientRect();
      if (!rect) return [0, 0];
      return [
        (clientX - rect.left - size.width / 2) / view.scale + view.x,
        (clientY - rect.top - size.height / 2) / view.scale + view.z,
      ];
    },
    [size, view],
  );

  const hitTest = useCallback(
    (world) => {
      const tolerance = HIT_TOLERANCE_PX / view.scale;
      for (const wall of doc.walls) {
        if (distanceToSegment(world, wall.start, wall.end) <= tolerance) return wall.id;
      }
      // Rooms tested after walls: a wall drawn on a room edge should win, since
      // it is the thinner target and the harder one to click.
      for (const room of doc.rooms) {
        if (pointInPolygon(world, room.polygon)) return room.id;
      }
      return null;
    },
    [doc, view.scale],
  );

  const handlePointerDown = useCallback(
    (event) => {
      if (event.button !== 0) return;
      const raw = toWorld(event.clientX, event.clientY);
      const world = [snap(raw[0]), snap(raw[1])];

      if (tool === 'select' || readOnly) {
        onSelect?.(hitTest(raw));
        return;
      }

      if (tool === 'wall') {
        if (!draft) setDraft({ kind: 'wall', start: world });
        else {
          onAddWall?.(draft.start, world);
          // Chain from the endpoint so a run of walls is one gesture per corner
          // rather than click-click, click-click round the whole outline.
          setDraft({ kind: 'wall', start: world });
        }
        return;
      }

      if (tool === 'room') {
        const points = draft?.kind === 'room' ? draft.points : [];
        // Closing on the first vertex finishes the polygon.
        if (points.length >= 3) {
          const [fx, fz] = points[0];
          if (Math.hypot(world[0] - fx, world[1] - fz) <= HIT_TOLERANCE_PX / view.scale) {
            onAddRoom?.(points, roomType);
            setDraft(null);
            return;
          }
        }
        setDraft({ kind: 'room', points: [...points, world] });
      }
    },
    [tool, readOnly, draft, roomType, toWorld, hitTest, onSelect, onAddWall, onAddRoom, view.scale],
  );

  const handlePointerMove = useCallback(
    (event) => {
      const raw = toWorld(event.clientX, event.clientY);
      setCursor([snap(raw[0]), snap(raw[1])]);
    },
    [toWorld],
  );

  const handleWheel = useCallback((event) => {
    // Zoom about the viewport centre. Anchoring to the pointer would be nicer
    // but needs the world point held fixed across the scale change; centre is
    // predictable and avoids drift when the wheel is spun quickly.
    setView((current) => {
      const next = current.scale * (event.deltaY < 0 ? 1.1 : 1 / 1.1);
      return { ...current, scale: Math.max(MIN_SCALE, Math.min(MAX_SCALE, next)) };
    });
  }, []);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') setDraft(null);
      if (event.key === 'Delete' || event.key === 'Backspace') {
        if (selectedId && !readOnly) {
          event.preventDefault();
          onRemove?.(selectedId);
        }
      }
      if (event.key === 'v') setTool('select');
      if (event.key === 'w') setTool('wall');
      if (event.key === 'r') setTool('room');
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selectedId, readOnly, onRemove]);

  // Grid lines only across the visible extent — drawing a fixed huge grid was
  // thousands of offscreen nodes on a zoomed-in plan.
  const grid = useMemo(() => {
    const step = view.scale < 20 ? GRID_M * 4 : GRID_M * 2;
    const halfW = size.width / 2 / view.scale;
    const halfH = size.height / 2 / view.scale;
    const lines = [];
    const startX = Math.floor((view.x - halfW) / step) * step;
    const endX = view.x + halfW;
    for (let x = startX; x <= endX; x += step) lines.push({ key: `x${x}`, x1: x, z1: view.z - halfH, x2: x, z2: view.z + halfH });
    const startZ = Math.floor((view.z - halfH) / step) * step;
    const endZ = view.z + halfH;
    for (let z = startZ; z <= endZ; z += step) lines.push({ key: `z${z}`, x1: view.x - halfW, z1: z, x2: view.x + halfW, z2: z });
    return lines;
  }, [view, size]);

  const empty = doc.walls.length === 0 && doc.rooms.length === 0;

  return (
    <div className={styles.wrap}>
      <div className={styles.toolbar} role="toolbar" aria-label="Floor plan tools">
        {TOOLS.map(({ key, label, Icon }) => (
          <button
            key={key}
            type="button"
            className={tool === key ? styles.toolActive : styles.tool}
            onClick={() => { setTool(key); setDraft(null); }}
            disabled={readOnly}
            aria-pressed={tool === key}
            title={`${label} (${key[0]})`}
          >
            <Icon aria-hidden="true" /> {label}
          </button>
        ))}

        {tool === 'room' && (
          <label className={styles.roomType}>
            Room
            <select value={roomType} onChange={(e) => setRoomType(e.target.value)} disabled={readOnly}>
              {ROOM_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
            </select>
          </label>
        )}

        <span className={styles.spacer} />
        <button type="button" className={styles.tool} onClick={onUndo} disabled={readOnly} title="Undo">
          <Undo2 aria-hidden="true" /> Undo
        </button>
        <button
          type="button"
          className={styles.tool}
          onClick={() => selectedId && onRemove?.(selectedId)}
          disabled={readOnly || !selectedId}
          title="Delete selection (Del)"
        >
          <Trash2 aria-hidden="true" /> Delete
        </button>
      </div>

      <svg
        ref={svgRef}
        className={styles.canvas}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onWheel={handleWheel}
        role="application"
        aria-label="Floor plan editor"
      >
        <g className={styles.grid}>
          {grid.map((line) => {
            const [x1, z1] = toScreen([line.x1, line.z1]);
            const [x2, z2] = toScreen([line.x2, line.z2]);
            return <line key={line.key} x1={x1} y1={z1} x2={x2} y2={z2} />;
          })}
        </g>

        {doc.rooms.map((room) => {
          const points = room.polygon.map((p) => toScreen(p).join(',')).join(' ');
          const centre = toScreen(
            room.polygon.reduce(
              (acc, p) => [acc[0] + p[0] / room.polygon.length, acc[1] + p[1] / room.polygon.length],
              [0, 0],
            ),
          );
          const sqft = Math.round(polygonArea(room.polygon) * 10.7639);
          return (
            <g key={room.id} className={selectedId === room.id ? styles.roomSelected : styles.room}>
              <polygon points={points} />
              <text x={centre[0]} y={centre[1]} textAnchor="middle">{room.name}</text>
              <text x={centre[0]} y={centre[1] + 14} textAnchor="middle" className={styles.dim}>
                {sqft} sq ft
              </text>
            </g>
          );
        })}

        {doc.walls.map((wall) => {
          const [x1, z1] = toScreen(wall.start);
          const [x2, z2] = toScreen(wall.end);
          const metres = wallLength(wall);
          return (
            <g key={wall.id} className={selectedId === wall.id ? styles.wallSelected : styles.wall}>
              <line x1={x1} y1={z1} x2={x2} y2={z2} strokeWidth={wall.interior ? 3 : 5} />
              {view.scale > 18 && (
                <text x={(x1 + x2) / 2} y={(z1 + z2) / 2 - 6} textAnchor="middle" className={styles.dim}>
                  {(metres * 3.28084).toFixed(1)}′
                </text>
              )}
            </g>
          );
        })}

        {draft?.kind === 'wall' && cursor && (
          <line
            className={styles.draft}
            x1={toScreen(draft.start)[0]} y1={toScreen(draft.start)[1]}
            x2={toScreen(cursor)[0]} y2={toScreen(cursor)[1]}
          />
        )}
        {draft?.kind === 'room' && draft.points.length > 0 && (
          <polyline
            className={styles.draft}
            points={[...draft.points, cursor].filter(Boolean).map((p) => toScreen(p).join(',')).join(' ')}
          />
        )}
      </svg>

      <div className={styles.hint}>
        {readOnly && 'Read only. '}
        {tool === 'wall' && 'Click each corner to run walls. Esc ends the run.'}
        {tool === 'room' && 'Click each corner, then click the first point again to close.'}
        {tool === 'select' && (empty
          ? 'Draw walls and rooms, or generate a shell from the building outline.'
          : 'Click to select. Del removes.')}
        {cursor && <span className={styles.readout}>{cursor[0].toFixed(1)}, {cursor[1].toFixed(1)} m</span>}
      </div>
    </div>
  );
}

export default FloorplanCanvas;
