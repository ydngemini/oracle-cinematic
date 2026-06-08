// ─── Oracle Spatial Tour — Penthouse Data Model ──────────────────────────────
// A single-floor luxury night penthouse. Coordinates are in metres.
// x = right, z = toward camera (south), y = up. Floor slab sits at y = 0.

export type Vec3 = [number, number, number]
export type Vec2 = [number, number] // [x, z] top-down

export interface CameraPose {
  position: Vec3
  target: Vec3
}

export interface Waypoint {
  id: string
  name: string
  /** short room descriptor shown in the floating label */
  tag: string
  /** FPV eye pose when standing in this room */
  pose: CameraPose
  /** 3D anchor for the clickable hotspot puck (on the floor) */
  hotspot: Vec3
  /** AI tour-guide narration line */
  narration: string
  /** top-down footprint for the minimap / floor-plan (centre + size in metres) */
  plan: { center: Vec2; size: Vec2 }
  /** stat chips surfaced in the room detail card */
  stats: { label: string; value: string }[]
}

// Pulled-back "dollhouse" overview pose
export const DOLLHOUSE_POSE: CameraPose = {
  position: [13, 13, 17],
  target: [0, 0.5, 0],
}

// Free-orbit default pose
export const EXPLORE_POSE: CameraPose = {
  position: [11, 6, 13],
  target: [0, 1.2, 0],
}

// Penthouse footprint, used by the floor slab + glass curtain wall
export const FOOTPRINT = { width: 22, depth: 18 }
export const CEILING_H = 3.2

export const WAYPOINTS: Waypoint[] = [
  {
    id: 'foyer',
    name: 'Private Foyer',
    tag: 'Entry',
    pose: { position: [-8.5, 1.65, 6], target: [-2, 1.4, 1] },
    hotspot: [-8.2, 0.04, 6],
    narration:
      'Welcome to the residence. A backlit onyx gallery wall greets you as the elevator opens directly into the private foyer.',
    plan: { center: [-8, 5], size: [4.5, 5] },
    stats: [
      { label: 'Entry', value: 'Private elevator' },
      { label: 'Ceilings', value: '3.2 m' },
    ],
  },
  {
    id: 'great',
    name: 'Great Room',
    tag: 'Living',
    pose: { position: [-1, 1.65, 5.4], target: [-1, 1.3, -3] },
    hotspot: [-1, 0.04, 5],
    narration:
      'The great room opens onto floor-to-ceiling glass with an unobstructed skyline panorama. Engineered oak floors run wall to wall.',
    plan: { center: [-1.5, 3], size: [9, 8] },
    stats: [
      { label: 'Area', value: '64 m²' },
      { label: 'Glass', value: 'Full-height curtain wall' },
      { label: 'View', value: 'Skyline · South' },
    ],
  },
  {
    id: 'kitchen',
    name: 'Chef’s Kitchen',
    tag: 'Kitchen',
    pose: { position: [6.5, 1.65, 4.5], target: [6.5, 1.2, -2] },
    hotspot: [6.5, 0.04, 4.2],
    narration:
      'A waterfall-edge marble island anchors the chef’s kitchen, wrapped in integrated appliances and a backlit stone backsplash.',
    plan: { center: [6.5, 4], size: [6, 6] },
    stats: [
      { label: 'Island', value: '3.4 m waterfall' },
      { label: 'Finish', value: 'Calacatta marble' },
    ],
  },
  {
    id: 'dining',
    name: 'Dining',
    tag: 'Dining',
    pose: { position: [6.5, 1.65, -3], target: [6.5, 1.2, -7.5] },
    hotspot: [6.5, 0.04, -3.2],
    narration:
      'The formal dining zone seats twelve beneath a suspended sculptural light installation.',
    plan: { center: [6.5, -4], size: [6, 6] },
    stats: [
      { label: 'Seats', value: '12' },
      { label: 'Lighting', value: 'Sculptural pendant' },
    ],
  },
  {
    id: 'master',
    name: 'Master Suite',
    tag: 'Bedroom',
    pose: { position: [-5, 1.65, -3.2], target: [-5, 1.2, -7.5] },
    hotspot: [-5, 0.04, -3.4],
    narration:
      'The master suite is a private wing with a floating upholstered platform bed and its own glass-wrapped corner exposure.',
    plan: { center: [-5.5, -4.5], size: [8, 6] },
    stats: [
      { label: 'Wing', value: 'Private' },
      { label: 'Exposure', value: 'Corner glass' },
      { label: 'Closet', value: 'Walk-in dressing' },
    ],
  },
  {
    id: 'terrace',
    name: 'Sky Terrace',
    tag: 'Outdoor',
    pose: { position: [0, 1.7, 7.4], target: [0, 1.4, 11] },
    hotspot: [0, 0.04, 7.6],
    narration:
      'Step out to the wrap-around sky terrace — an infinity-edge plunge pool and lounge suspended above the city lights.',
    plan: { center: [0, 8.2], size: [14, 3] },
    stats: [
      { label: 'Terrace', value: 'Wrap-around' },
      { label: 'Feature', value: 'Infinity plunge pool' },
    ],
  },
]

export const WAYPOINT_INDEX: Record<string, Waypoint> = Object.fromEntries(
  WAYPOINTS.map((w) => [w.id, w])
)

// Adjacency for "walk to next room" hotspot arrows (Matterport-style pathing)
export const TOUR_ORDER = ['foyer', 'great', 'terrace', 'kitchen', 'dining', 'master']
