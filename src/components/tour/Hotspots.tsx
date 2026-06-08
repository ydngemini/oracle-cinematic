'use client'

import { useState } from 'react'
import { Html } from '@react-three/drei'
import { WAYPOINTS } from './tourData'
import styles from './tour.module.css'

export function Hotspots({
  activeId,
  onNavigate,
  visible,
}: {
  activeId: string | null
  onNavigate: (id: string) => void
  visible: boolean
}) {
  const [hovered, setHovered] = useState<string | null>(null)
  if (!visible) return null

  return (
    <>
      {WAYPOINTS.map((wp) => {
        const isActive = wp.id === activeId
        return (
          <Html
            key={wp.id}
            position={wp.hotspot}
            center
            distanceFactor={9}
            zIndexRange={[40, 0]}
            className={styles.hotspotWrap}
          >
            <button
              type="button"
              aria-label={`Go to ${wp.name}`}
              className={`${styles.hotspot} ${isActive ? styles.hotspotActive : ''}`}
              onPointerEnter={() => setHovered(wp.id)}
              onPointerLeave={() => setHovered(null)}
              onClick={() => onNavigate(wp.id)}
            >
              <span className={styles.hotspotRing} />
              <span className={styles.hotspotCore} />
              {(hovered === wp.id || isActive) && (
                <span className={styles.hotspotLabel}>
                  <span className={styles.hotspotTag}>{wp.tag}</span>
                  {wp.name}
                </span>
              )}
            </button>
          </Html>
        )
      })}
    </>
  )
}
