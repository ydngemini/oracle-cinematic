'use client'

import { useRef, useMemo } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import { EffectComposer, Bloom } from '@react-three/postprocessing'
import * as THREE from 'three'

const RING_COUNT = 5

function EnergyField() {
  const groupRef = useRef<THREE.Group>(null)
  const particlesRef = useRef<THREE.Points>(null)

  const particlePositions = useMemo(() => {
    const positions = new Float32Array(500 * 3)
    for (let i = 0; i < 500; i++) {
      const theta = Math.random() * Math.PI * 2
      const phi = Math.acos(2 * Math.random() - 1)
      const r = 3 + Math.random() * 2
      positions[i * 3] = r * Math.sin(phi) * Math.cos(theta)
      positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta)
      positions[i * 3 + 2] = r * Math.cos(phi)
    }
    return positions
  }, [])

  useFrame((state) => {
    if (!groupRef.current) return
    const t = state.clock.elapsedTime

    groupRef.current.children.forEach((child, i) => {
      if (child instanceof THREE.Mesh) {
        child.rotation.x = t * 0.1 * (i + 1) * 0.3
        child.rotation.y = t * 0.15 * (i + 1) * 0.2
        child.rotation.z = Math.sin(t * 0.5 + i) * 0.3
      }
    })

    if (particlesRef.current) {
      particlesRef.current.rotation.y = t * 0.05
      particlesRef.current.rotation.x = Math.sin(t * 0.1) * 0.1
    }
  })

  return (
    <>
      <group ref={groupRef}>
        {Array.from({ length: RING_COUNT }).map((_, i) => (
          <mesh key={i}>
            <torusGeometry args={[2 + i * 0.6, 0.005, 8, 128]} />
            <meshBasicMaterial
              color={i % 2 === 0 ? '#00f0ff' : '#8b5cf6'}
              transparent
              opacity={0.2 - i * 0.03}
            />
          </mesh>
        ))}
      </group>

      <points ref={particlesRef}>
        <bufferGeometry>
          <bufferAttribute
            attach="attributes-position"
            args={[particlePositions, 3]}
          />
        </bufferGeometry>
        <pointsMaterial
          size={0.02}
          color="#00f0ff"
          transparent
          opacity={0.4}
          blending={THREE.AdditiveBlending}
          depthWrite={false}
        />
      </points>
    </>
  )
}

export function CTAScene() {
  return (
    <Canvas
      camera={{ position: [0, 0, 8], fov: 45 }}
      dpr={[1, 1.5]}
      gl={{ preserveDrawingBuffer: true }}
      style={{ position: 'absolute', inset: 0 }}
    >
      <ambientLight intensity={0.05} />
      <EnergyField />
      <EffectComposer>
        <Bloom intensity={1.5} luminanceThreshold={0.1} mipmapBlur />
      </EffectComposer>
    </Canvas>
  )
}
