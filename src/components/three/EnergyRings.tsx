'use client'

import { useRef } from 'react'
import { useFrame } from '@react-three/fiber'
import * as THREE from 'three'

export function EnergyRings() {
  const group = useRef<THREE.Group>(null)

  useFrame((state) => {
    if (!group.current) return
    const t = state.clock.elapsedTime

    group.current.children.forEach((ring, i) => {
      ring.rotation.x = Math.sin(t * 0.3 + i * 0.5) * 0.3
      ring.rotation.y = t * (0.1 + i * 0.05)
      ring.rotation.z = Math.cos(t * 0.2 + i * 0.7) * 0.2
    })
  })

  return (
    <group ref={group}>
      {[2.2, 2.8, 3.4].map((radius, i) => (
        <mesh key={i}>
          <torusGeometry args={[radius, 0.008, 16, 100]} />
          <meshBasicMaterial
            color={i === 0 ? '#00f0ff' : i === 1 ? '#4060ff' : '#8b5cf6'}
            transparent
            opacity={0.4 - i * 0.1}
          />
        </mesh>
      ))}
    </group>
  )
}
