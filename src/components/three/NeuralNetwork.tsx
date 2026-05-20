'use client'

import { useRef, useMemo } from 'react'
import { Canvas, useFrame, extend } from '@react-three/fiber'
import { Float, Line } from '@react-three/drei'
import { EffectComposer, Bloom } from '@react-three/postprocessing'
import * as THREE from 'three'

function Node({ position, color }: { position: [number, number, number]; color: string }) {
  const meshRef = useRef<THREE.Mesh>(null)

  useFrame((state) => {
    if (!meshRef.current) return
    meshRef.current.scale.setScalar(
      1 + Math.sin(state.clock.elapsedTime * 2 + position[0]) * 0.1
    )
  })

  return (
    <Float speed={2} floatIntensity={0.3}>
      <mesh ref={meshRef} position={position}>
        <sphereGeometry args={[0.08, 16, 16]} />
        <meshBasicMaterial color={color} />
      </mesh>
    </Float>
  )
}

function Connections({ nodes }: { nodes: [number, number, number][] }) {
  const linesRef = useRef<THREE.Group>(null)

  const connections = useMemo(() => {
    const conns: { start: THREE.Vector3; end: THREE.Vector3 }[] = []
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dist = new THREE.Vector3(...nodes[i]).distanceTo(new THREE.Vector3(...nodes[j]))
        if (dist < 3) {
          conns.push({
            start: new THREE.Vector3(...nodes[i]),
            end: new THREE.Vector3(...nodes[j]),
          })
        }
      }
    }
    return conns
  }, [nodes])

  useFrame((state) => {
    if (!linesRef.current) return
    linesRef.current.children.forEach((line, i) => {
      const mat = (line as THREE.Line).material as THREE.LineBasicMaterial
      mat.opacity = 0.1 + Math.sin(state.clock.elapsedTime * 1.5 + i * 0.3) * 0.08
    })
  })

  return (
    <group ref={linesRef}>
      {connections.map((conn, i) => (
        <Line
          key={i}
          points={[conn.start.toArray(), conn.end.toArray()]}
          color="#00f0ff"
          transparent
          opacity={0.15}
          lineWidth={1}
        />
      ))}
    </group>
  )
}

function NetworkScene() {
  const nodes: [number, number, number][] = useMemo(() => {
    const pts: [number, number, number][] = []
    for (let i = 0; i < 40; i++) {
      pts.push([
        (Math.random() - 0.5) * 8,
        (Math.random() - 0.5) * 5,
        (Math.random() - 0.5) * 4,
      ])
    }
    return pts
  }, [])

  const colors = ['#00f0ff', '#4060ff', '#8b5cf6', '#ff3366']

  return (
    <>
      <ambientLight intensity={0.2} />
      {nodes.map((pos, i) => (
        <Node key={i} position={pos} color={colors[i % colors.length]} />
      ))}
      <Connections nodes={nodes} />
      <EffectComposer>
        <Bloom intensity={2} luminanceThreshold={0.1} mipmapBlur />
      </EffectComposer>
    </>
  )
}

export function NeuralNetwork() {
  return (
    <Canvas
      camera={{ position: [0, 0, 7], fov: 50 }}
      dpr={[1, 1.5]}
      style={{ position: 'absolute', inset: 0 }}
    >
      <NetworkScene />
    </Canvas>
  )
}
