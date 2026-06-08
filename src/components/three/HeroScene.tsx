'use client'

import { Suspense } from 'react'
import { Canvas } from '@react-three/fiber'
import { EffectComposer, Bloom, ChromaticAberration, Vignette } from '@react-three/postprocessing'
import { BlendFunction } from 'postprocessing'
import { Float, Stars } from '@react-three/drei'
import { NeuralOrb } from './NeuralOrb'
import { ParticleField } from './ParticleField'
import { EnergyRings } from './EnergyRings'
import { HolographicGrid } from './HolographicGrid'
import * as THREE from 'three'

// Hoisted to module scope — never re-allocated, no GC churn
const CHROMATIC_OFFSET = new THREE.Vector2(0.0005, 0.0005)

interface HeroSceneProps {
  mouseX: number
  mouseY: number
}

export function HeroScene({ mouseX, mouseY }: HeroSceneProps) {
  return (
    <Canvas
      camera={{ position: [0, 0, 6], fov: 55 }}
      gl={{
        antialias: true,
        toneMapping: THREE.ACESFilmicToneMapping,
        toneMappingExposure: 1.2,
      }}
      dpr={[1, 2]}
      style={{ position: 'absolute', inset: 0 }}
    >
      <Suspense fallback={null}>
        <ambientLight intensity={0.1} />
        <pointLight position={[5, 5, 5]} intensity={0.5} color="#00f0ff" />
        <pointLight position={[-5, -3, 3]} intensity={0.3} color="#8b5cf6" />
        <pointLight position={[0, -5, 2]} intensity={0.2} color="#ff3366" />

        <Float speed={1.5} rotationIntensity={0.3} floatIntensity={0.5}>
          <NeuralOrb mouseX={mouseX} mouseY={mouseY} />
        </Float>

        <EnergyRings />
        <ParticleField />
        <HolographicGrid />
        <Stars radius={50} depth={50} count={1000} factor={2} fade speed={0.5} />

        <EffectComposer multisampling={0}>
          <Bloom
            intensity={1.5}
            luminanceThreshold={0.1}
            luminanceSmoothing={0.9}
            mipmapBlur
          />
          <ChromaticAberration
            blendFunction={BlendFunction.NORMAL}
            offset={CHROMATIC_OFFSET}
          />
          <Vignette darkness={0.7} offset={0.2} />
        </EffectComposer>
      </Suspense>
    </Canvas>
  )
}
