/**
 * Rehab cost model — turns spatial metrics into line items.
 *
 * Every line item emits in the shape `calculate_underwriting` already accepts
 * (backend/intelligence_engine.py): { category, quantity, unit_cost, basis }.
 * The backend recomputes `rehab = Σ(quantity × unit_cost)` in Decimal and feeds
 * MAO. The browser total below is a PREVIEW ONLY — the server value is
 * authoritative, because float money in JS is not.
 *
 * Unit costs are national-median placeholders. They are deliberately declared
 * here as data, not scattered through the UI, so a per-market table can replace
 * this module without touching the hook or the drawer.
 */

import type { ImperialMetrics } from './metrics';
import type { RoomType } from './protocol';

export type CostDriver =
  | 'wall_face_sqft'   // drywall, paint — both faces of interior walls
  | 'wall_linear_ft'   // framing, top/bottom plate
  | 'floor_sqft'       // flooring, subfloor
  | 'room_perimeter_ft'// baseboard, trim
  | 'per_room'         // per-room-type fixture packages
  | 'per_door'
  | 'per_window';

export interface CostLine {
  /** Stable key for React lists and for diffing against a saved estimate. */
  key: string;
  /** Human label shown in the drawer. */
  label: string;
  /** Maps to `category` in the underwriting payload. */
  category: string;
  driver: CostDriver;
  /** Only for driver === 'per_room'. */
  roomType?: RoomType;
  unit: string;
  unitCost: number;
  /** Free-text justification carried into the underwriting trace. */
  basis: string;
  /** Off by default keeps the estimate conservative until the agent opts in. */
  enabledByDefault: boolean;
}

export const DEFAULT_COST_LINES: readonly CostLine[] = [
  {
    key: 'framing',
    label: 'Wall framing',
    category: 'Framing',
    driver: 'wall_linear_ft',
    unit: 'lin ft',
    unitCost: 14,
    basis: 'Derived from 3D layout: total wall centreline length.',
    enabledByDefault: true,
  },
  {
    key: 'drywall',
    label: 'Drywall — hang, tape, finish',
    category: 'Drywall',
    driver: 'wall_face_sqft',
    unit: 'sq ft face',
    unitCost: 2.6,
    basis: 'Derived from 3D layout: wall face area, interior walls counted on both faces.',
    enabledByDefault: true,
  },
  {
    key: 'paint',
    label: 'Interior paint',
    category: 'Paint',
    driver: 'wall_face_sqft',
    unit: 'sq ft face',
    unitCost: 1.15,
    basis: 'Derived from 3D layout: wall face area.',
    enabledByDefault: true,
  },
  {
    key: 'flooring',
    label: 'Flooring',
    category: 'Flooring',
    driver: 'floor_sqft',
    unit: 'sq ft',
    unitCost: 6.5,
    basis: 'Derived from 3D layout: sum of room polygon areas.',
    enabledByDefault: true,
  },
  {
    key: 'trim',
    label: 'Baseboard & trim',
    category: 'Trim',
    driver: 'room_perimeter_ft',
    unit: 'lin ft',
    unitCost: 4.25,
    basis: 'Derived from 3D layout: sum of room perimeters.',
    enabledByDefault: true,
  },
  {
    key: 'doors',
    label: 'Interior doors — supply & hang',
    category: 'Doors',
    driver: 'per_door',
    unit: 'each',
    unitCost: 385,
    basis: 'Derived from 3D layout: door openings placed in walls.',
    enabledByDefault: true,
  },
  {
    key: 'windows',
    label: 'Window units',
    category: 'Windows',
    driver: 'per_window',
    unit: 'each',
    unitCost: 720,
    basis: 'Derived from 3D layout: window openings placed in walls.',
    enabledByDefault: true,
  },
  {
    key: 'kitchen',
    label: 'Kitchen package — cabinets, counters, appliances',
    category: 'Kitchen',
    driver: 'per_room',
    roomType: 'kitchen',
    unit: 'room',
    unitCost: 18500,
    basis: 'Derived from 3D layout: rooms typed as kitchen.',
    enabledByDefault: true,
  },
  {
    key: 'bathroom',
    label: 'Bathroom package — fixtures, tile, vanity',
    category: 'Bathroom',
    driver: 'per_room',
    roomType: 'bathroom',
    unit: 'room',
    unitCost: 11200,
    basis: 'Derived from 3D layout: rooms typed as bathroom.',
    enabledByDefault: true,
  },
  {
    key: 'utility',
    label: 'Utility / laundry hookups',
    category: 'Utility',
    driver: 'per_room',
    roomType: 'utility',
    unit: 'room',
    unitCost: 3400,
    basis: 'Derived from 3D layout: rooms typed as utility.',
    enabledByDefault: false,
  },
  {
    key: 'hvac',
    label: 'HVAC — distribution by conditioned area',
    category: 'HVAC',
    driver: 'floor_sqft',
    unit: 'sq ft',
    unitCost: 9.0,
    basis: 'Derived from 3D layout: conditioned floor area.',
    enabledByDefault: false,
  },
  {
    key: 'electrical',
    label: 'Electrical rough-in & devices',
    category: 'Electrical',
    driver: 'floor_sqft',
    unit: 'sq ft',
    unitCost: 7.5,
    basis: 'Derived from 3D layout: floor area.',
    enabledByDefault: false,
  },
] as const;

/** Resolve the quantity a line item bills against, from imperial metrics. */
export function quantityFor(line: CostLine, m: ImperialMetrics): number {
  switch (line.driver) {
    case 'wall_linear_ft':    return m.wall_linear_ft;
    case 'wall_face_sqft':    return m.wall_face_area_sqft;
    case 'floor_sqft':        return m.floor_area_sqft;
    case 'room_perimeter_ft': return m.room_perimeter_ft;
    case 'per_door':          return m.counts.doors;
    case 'per_window':        return m.counts.windows;
    case 'per_room':          return line.roomType ? (m.counts.by_room_type[line.roomType] ?? 0) : 0;
    default:                  return 0;
  }
}

/** Quantities are rounded for display/billing sanity: whole units for counts. */
export function roundQuantity(line: CostLine, raw: number): number {
  const isCount = line.driver === 'per_door' || line.driver === 'per_window' || line.driver === 'per_room';
  return isCount ? Math.round(raw) : Math.round(raw * 10) / 10;
}
