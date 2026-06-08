'use client'

import { WAYPOINTS, FOOTPRINT, type Vec2 } from './tourData'

const BOUNDS = { minX: -12, maxX: 12, minZ: -10, maxZ: 12 }

function project(x: number, z: number, w: number, h: number): Vec2 {
  const sx = ((x - BOUNDS.minX) / (BOUNDS.maxX - BOUNDS.minX)) * w
  const sy = ((z - BOUNDS.minZ) / (BOUNDS.maxZ - BOUNDS.minZ)) * h
  return [sx, sy]
}

export function Minimap({
  activeId,
  currentPos,
  onNavigate,
  large = false,
}: {
  activeId: string | null
  currentPos: Vec2
  onNavigate: (id: string) => void
  large?: boolean
}) {
  const W = large ? 460 : 208
  const H = large ? 422 : 190
  const sx = W / (BOUNDS.maxX - BOUNDS.minX)
  const [px, py] = project(currentPos[0], currentPos[1], W, H)

  // building outline
  const [bx, by] = project(-FOOTPRINT.width / 2, -FOOTPRINT.depth / 2, W, H)
  const bw = FOOTPRINT.width * sx
  const bh = FOOTPRINT.depth * (H / (BOUNDS.maxZ - BOUNDS.minZ))

  return (
    <svg
      width={W}
      height={H}
      viewBox={`0 0 ${W} ${H}`}
      style={{ display: 'block', width: '100%', height: 'auto' }}
    >
      <defs>
        <radialGradient id="posGlow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#00f0ff" stopOpacity="0.9" />
          <stop offset="100%" stopColor="#00f0ff" stopOpacity="0" />
        </radialGradient>
      </defs>

      {/* building footprint */}
      <rect
        x={bx}
        y={by}
        width={bw}
        height={bh}
        rx={6}
        fill="rgba(0,240,255,0.03)"
        stroke="rgba(255,255,255,0.10)"
        strokeWidth={1}
      />

      {/* rooms */}
      {WAYPOINTS.map((wp) => {
        const [cx, cz] = wp.plan.center
        const [w, d] = wp.plan.size
        const [rx, ry] = project(cx - w / 2, cz - d / 2, W, H)
        const rw = w * sx
        const rh = d * (H / (BOUNDS.maxZ - BOUNDS.minZ))
        const isActive = wp.id === activeId
        const [lx, ly] = project(cx, cz, W, H)
        return (
          <g
            key={wp.id}
            onClick={() => onNavigate(wp.id)}
            style={{ cursor: 'pointer' }}
          >
            <rect
              x={rx}
              y={ry}
              width={rw}
              height={rh}
              rx={4}
              fill={isActive ? 'rgba(139,92,246,0.22)' : 'rgba(255,255,255,0.04)'}
              stroke={isActive ? 'rgba(139,92,246,0.9)' : 'rgba(255,255,255,0.12)'}
              strokeWidth={isActive ? 1.5 : 1}
            />
            {large && (
              <text
                x={lx}
                y={ly}
                textAnchor="middle"
                dominantBaseline="middle"
                fontSize={11}
                fontWeight={600}
                fill={isActive ? '#e0e8ff' : 'rgba(224,232,255,0.6)'}
              >
                {wp.name}
              </text>
            )}
          </g>
        )
      })}

      {/* live position marker */}
      <circle cx={px} cy={py} r={large ? 22 : 13} fill="url(#posGlow)" />
      <circle cx={px} cy={py} r={large ? 6 : 4} fill="#00f0ff" stroke="#fff" strokeWidth={1.5} />
    </svg>
  )
}
