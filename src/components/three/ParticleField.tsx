'use client'

import { useRef, useMemo } from 'react'
import { useFrame } from '@react-three/fiber'
import * as THREE from 'three'

const PARTICLE_COUNT = 2000

export function ParticleField() {
  const pointsRef = useRef<THREE.Points>(null)

  const { positions, velocities } = useMemo(() => {
    const positions = new Float32Array(PARTICLE_COUNT * 3)
    const velocities = new Float32Array(PARTICLE_COUNT * 3)

    for (let i = 0; i < PARTICLE_COUNT; i++) {
      const i3 = i * 3
      positions[i3] = (Math.random() - 0.5) * 20
      positions[i3 + 1] = (Math.random() - 0.5) * 20
      positions[i3 + 2] = (Math.random() - 0.5) * 20

      velocities[i3] = (Math.random() - 0.5) * 0.002
      velocities[i3 + 1] = (Math.random() - 0.5) * 0.002
      velocities[i3 + 2] = (Math.random() - 0.5) * 0.002
    }

    return { positions, velocities }
  }, [])

  useFrame(() => {
    if (!pointsRef.current) return
    const posArray = pointsRef.current.geometry.attributes.position.array as Float32Array

    for (let i = 0; i < PARTICLE_COUNT; i++) {
      const i3 = i * 3
      posArray[i3] += velocities[i3]
      posArray[i3 + 1] += velocities[i3 + 1]
      posArray[i3 + 2] += velocities[i3 + 2]

      if (Math.abs(posArray[i3]) > 10) velocities[i3] *= -1
      if (Math.abs(posArray[i3 + 1]) > 10) velocities[i3 + 1] *= -1
      if (Math.abs(posArray[i3 + 2]) > 10) velocities[i3 + 2] *= -1
    }

    pointsRef.current.geometry.attributes.position.needsUpdate = true
  })

  return (
    <points ref={pointsRef}>
      <bufferGeometry>
        <bufferAttribute
          attach="attributes-position"
          args={[positions, 3]}
        />
      </bufferGeometry>
      <pointsMaterial
        size={0.015}
        color="#00f0ff"
        transparent
        opacity={0.6}
        sizeAttenuation
        blending={THREE.AdditiveBlending}
        depthWrite={false}
      />
    </points>
  )
}
