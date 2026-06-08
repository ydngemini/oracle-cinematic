'use client'

import { useMemo, useRef } from 'react'
import { useFrame } from '@react-three/fiber'
import {
  RoundedBox,
  MeshReflectorMaterial,
  ContactShadows,
  Stars,
  Sparkles,
} from '@react-three/drei'
import * as THREE from 'three'
import { FOOTPRINT, CEILING_H, type Vec3 } from './tourData'

// ─── Reusable soft-edged solid ───────────────────────────────────────────────
function Solid({
  position,
  size,
  color,
  metalness = 0.2,
  roughness = 0.5,
  emissive,
  emissiveIntensity = 0,
  radius = 0.04,
  castShadow = true,
}: {
  position: Vec3
  size: Vec3
  color: string
  metalness?: number
  roughness?: number
  emissive?: string
  emissiveIntensity?: number
  radius?: number
  castShadow?: boolean
}) {
  return (
    <RoundedBox
      position={position}
      args={size}
      radius={radius}
      smoothness={3}
      castShadow={castShadow}
      receiveShadow
    >
      <meshStandardMaterial
        color={color}
        metalness={metalness}
        roughness={roughness}
        emissive={emissive ?? '#000000'}
        emissiveIntensity={emissiveIntensity}
      />
    </RoundedBox>
  )
}

// ─── Reflective floor slab ───────────────────────────────────────────────────
function Floor() {
  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0, 0]} receiveShadow>
      <planeGeometry args={[FOOTPRINT.width, FOOTPRINT.depth]} />
      <MeshReflectorMaterial
        resolution={1024}
        mixBlur={1}
        mixStrength={4}
        blur={[400, 100]}
        roughness={0.85}
        depthScale={1.1}
        minDepthThreshold={0.4}
        maxDepthThreshold={1.3}
        color="#0b0d12"
        metalness={0.6}
      />
    </mesh>
  )
}

// ─── Perimeter glass curtain wall + ceiling light coves ──────────────────────
function Shell() {
  const w = FOOTPRINT.width / 2
  const d = FOOTPRINT.depth / 2
  const glass = (
    <meshPhysicalMaterial
      color="#0a1622"
      transparent
      opacity={0.16}
      roughness={0.05}
      metalness={0}
      transmission={0.0}
      side={THREE.DoubleSide}
    />
  )
  return (
    <group>
      {/* four glass walls */}
      <mesh position={[0, CEILING_H / 2, -d]}>
        <planeGeometry args={[FOOTPRINT.width, CEILING_H]} />
        {glass}
      </mesh>
      <mesh position={[0, CEILING_H / 2, d]} rotation={[0, Math.PI, 0]}>
        <planeGeometry args={[FOOTPRINT.width, CEILING_H]} />
        {glass}
      </mesh>
      <mesh position={[-w, CEILING_H / 2, 0]} rotation={[0, Math.PI / 2, 0]}>
        <planeGeometry args={[FOOTPRINT.depth, CEILING_H]} />
        {glass}
      </mesh>
      <mesh position={[w, CEILING_H / 2, 0]} rotation={[0, -Math.PI / 2, 0]}>
        <planeGeometry args={[FOOTPRINT.depth, CEILING_H]} />
        {glass}
      </mesh>

      {/* ceiling */}
      <mesh position={[0, CEILING_H, 0]} rotation={[Math.PI / 2, 0, 0]} receiveShadow>
        <planeGeometry args={[FOOTPRINT.width, FOOTPRINT.depth]} />
        <meshStandardMaterial color="#0c0c12" roughness={0.9} metalness={0.1} />
      </mesh>

      {/* recessed emissive light coves (run along x) */}
      {[-4.5, 1.5].map((z) => (
        <Solid
          key={z}
          position={[0, CEILING_H - 0.06, z]}
          size={[FOOTPRINT.width - 2, 0.06, 0.22]}
          color="#fff2dc"
          emissive="#ffd9a0"
          emissiveIntensity={2.4}
          metalness={0}
          roughness={1}
          castShadow={false}
        />
      ))}
    </group>
  )
}

// ─── Interior partition for the master wing ──────────────────────────────────
function Partitions() {
  return (
    <group>
      {/* wall separating master wing (runs along x at z = -1.5) */}
      <Solid
        position={[-5.5, CEILING_H / 2, -1.4]}
        size={[9, CEILING_H, 0.18]}
        color="#15161d"
        metalness={0.1}
        roughness={0.8}
        castShadow={false}
      />
      {/* perpendicular wall closing the suite */}
      <Solid
        position={[-1.1, CEILING_H / 2, -4.4]}
        size={[0.18, CEILING_H, 6]}
        color="#15161d"
        metalness={0.1}
        roughness={0.8}
        castShadow={false}
      />
      {/* backlit onyx feature wall in the foyer */}
      <Solid
        position={[-10.6, CEILING_H / 2, 5]}
        size={[0.16, CEILING_H, 5]}
        color="#2a2030"
        emissive="#5a2a7a"
        emissiveIntensity={0.6}
        metalness={0.3}
        roughness={0.4}
        castShadow={false}
      />
    </group>
  )
}

// ─── Living-room cluster ─────────────────────────────────────────────────────
function LivingCluster() {
  return (
    <group position={[-1.5, 0, 2.5]}>
      {/* rug */}
      <Solid position={[0, 0.01, 0]} size={[5.2, 0.02, 4]} color="#1b1c24" roughness={1} metalness={0} radius={0.01} castShadow={false} />
      {/* sofa (L-shape via two solids) */}
      <Solid position={[0, 0.4, -1.6]} size={[4.4, 0.8, 1]} color="#2c2e3a" roughness={0.9} metalness={0} radius={0.12} />
      <Solid position={[-2.2, 0.4, -0.2]} size={[1, 0.8, 2.8]} color="#2c2e3a" roughness={0.9} metalness={0} radius={0.12} />
      {/* back cushions */}
      <Solid position={[0, 0.85, -2]} size={[4.4, 0.55, 0.3]} color="#33353f" roughness={0.9} metalness={0} radius={0.1} castShadow={false} />
      {/* coffee table */}
      <Solid position={[0, 0.22, 0.2]} size={[1.8, 0.12, 0.9]} color="#0e0f14" metalness={0.7} roughness={0.2} radius={0.03} />
      {/* media console + emissive screen */}
      <Solid position={[0, 0.35, 2.4]} size={[4, 0.5, 0.4]} color="#101118" metalness={0.5} roughness={0.4} />
      <Solid position={[0, 1.5, 2.62]} size={[3.4, 1.6, 0.06]} color="#05060a" emissive="#0a2740" emissiveIntensity={1.2} metalness={0.2} roughness={0.3} castShadow={false} />
    </group>
  )
}

// ─── Kitchen cluster ─────────────────────────────────────────────────────────
function KitchenCluster() {
  return (
    <group position={[6.5, 0, 4]}>
      {/* waterfall island */}
      <Solid position={[0, 0.45, 0]} size={[3.4, 0.9, 1.3]} color="#e9e6df" metalness={0.1} roughness={0.25} radius={0.02} />
      <Solid position={[0, 0.95, 0]} size={[3.6, 0.08, 1.5]} color="#f4f2ee" metalness={0.1} roughness={0.15} radius={0.02} castShadow={false} />
      {/* back counter run */}
      <Solid position={[0, 0.45, 2.4]} size={[5.4, 0.9, 0.7]} color="#16171f" metalness={0.4} roughness={0.4} />
      {/* backlit backsplash */}
      <Solid position={[0, 1.7, 2.78]} size={[5.4, 1.4, 0.05]} color="#23252e" emissive="#1a3a4a" emissiveIntensity={0.7} metalness={0.3} roughness={0.3} castShadow={false} />
      {/* stools */}
      {[-1, 0.2, 1.4].map((x) => (
        <Solid key={x} position={[x, 0.35, -1.1]} size={[0.4, 0.7, 0.4]} color="#1d1e26" metalness={0.3} roughness={0.6} radius={0.06} />
      ))}
    </group>
  )
}

// ─── Dining cluster ──────────────────────────────────────────────────────────
function DiningCluster() {
  return (
    <group position={[6.5, 0, -4.5]}>
      <Solid position={[0, 0.38, 0]} size={[3.6, 0.1, 1.3]} color="#0d0e13" metalness={0.6} roughness={0.25} radius={0.02} />
      {/* legs */}
      {[[-1.6, -0.5], [1.6, -0.5], [-1.6, 0.5], [1.6, 0.5]].map(([x, z], i) => (
        <Solid key={i} position={[x, 0.19, z]} size={[0.1, 0.38, 0.1]} color="#0d0e13" metalness={0.7} roughness={0.2} castShadow={false} />
      ))}
      {/* chairs */}
      {[-1.2, 0, 1.2].flatMap((x) =>
        [-0.95, 0.95].map((z) => (
          <Solid key={`${x}-${z}`} position={[x, 0.45, z]} size={[0.45, 0.9, 0.45]} color="#23242d" metalness={0.2} roughness={0.7} radius={0.05} />
        ))
      )}
      {/* sculptural pendant */}
      <Solid position={[0, 2.5, 0]} size={[1.8, 0.12, 0.5]} color="#fff2dc" emissive="#ffcaa0" emissiveIntensity={2.2} metalness={0} roughness={1} castShadow={false} radius={0.06} />
    </group>
  )
}

// ─── Master suite cluster ────────────────────────────────────────────────────
function BedroomCluster() {
  return (
    <group position={[-5.5, 0, -4.8]}>
      {/* platform */}
      <Solid position={[0, 0.18, 0]} size={[4, 0.35, 3]} color="#101119" metalness={0.3} roughness={0.5} radius={0.03} />
      {/* mattress */}
      <Solid position={[0, 0.5, 0.1]} size={[3, 0.4, 2.4]} color="#3a3c48" metalness={0} roughness={0.95} radius={0.1} castShadow={false} />
      {/* headboard */}
      <Solid position={[0, 1, -1.4]} size={[3.4, 1.4, 0.2]} color="#26272f" metalness={0.1} roughness={0.8} radius={0.05} />
      {/* nightstands w/ glow */}
      {[-1.9, 1.9].map((x) => (
        <group key={x}>
          <Solid position={[x, 0.35, -1]} size={[0.7, 0.5, 0.7]} color="#15161d" metalness={0.3} roughness={0.5} radius={0.04} />
          <Solid position={[x, 0.72, -1]} size={[0.3, 0.06, 0.3]} color="#fff2dc" emissive="#ffd9a0" emissiveIntensity={2} castShadow={false} radius={0.02} />
        </group>
      ))}
    </group>
  )
}

// ─── Sky terrace: infinity pool strip + loungers ─────────────────────────────
function Terrace() {
  const water = useRef<THREE.Mesh>(null)
  useFrame((state) => {
    if (water.current) {
      const m = water.current.material as THREE.MeshStandardMaterial
      m.emissiveIntensity = 0.5 + Math.sin(state.clock.elapsedTime * 1.5) * 0.12
    }
  })
  return (
    <group position={[0, 0, 8]}>
      {/* deck */}
      <Solid position={[0, 0.02, 0]} size={[14, 0.04, 3]} color="#15161c" roughness={1} metalness={0} radius={0.01} castShadow={false} />
      {/* pool basin */}
      <Solid position={[0, -0.02, 0.4]} size={[8, 0.2, 1.6]} color="#04121c" metalness={0.2} roughness={0.3} castShadow={false} />
      {/* glowing water surface */}
      <mesh ref={water} position={[0, 0.06, 0.4]} rotation={[-Math.PI / 2, 0, 0]}>
        <planeGeometry args={[7.8, 1.5]} />
        <meshStandardMaterial color="#0a3a52" emissive="#1fb6ff" emissiveIntensity={0.5} metalness={0.6} roughness={0.15} transparent opacity={0.92} />
      </mesh>
      {/* loungers */}
      {[-5, 5].map((x) => (
        <Solid key={x} position={[x, 0.25, -0.9]} size={[1, 0.4, 2]} color="#2a2c36" metalness={0} roughness={0.9} radius={0.08} />
      ))}
    </group>
  )
}

// ─── Night skyline backdrop ──────────────────────────────────────────────────
function Skyline() {
  const towers = useMemo(() => {
    const out: { pos: Vec3; size: Vec3; glow: number }[] = []
    const rng = (s: number) => {
      const x = Math.sin(s * 99.13) * 43758.5453
      return x - Math.floor(x)
    }
    for (let i = 0; i < 70; i++) {
      const ang = (i / 70) * Math.PI * 2
      const r = 38 + rng(i) * 26
      const h = 8 + rng(i * 2.3) * 34
      out.push({
        pos: [Math.cos(ang) * r, h / 2 - 6, Math.sin(ang) * r],
        size: [3 + rng(i * 5) * 4, h, 3 + rng(i * 7) * 4],
        glow: 0.15 + rng(i * 3) * 0.5,
      })
    }
    return out
  }, [])
  return (
    <group>
      {towers.map((t, i) => (
        <mesh key={i} position={t.pos}>
          <boxGeometry args={t.size} />
          <meshStandardMaterial
            color="#070a12"
            emissive="#1a2a4a"
            emissiveIntensity={t.glow}
            metalness={0.2}
            roughness={0.8}
          />
        </mesh>
      ))}
    </group>
  )
}

// ─── Lighting rig ────────────────────────────────────────────────────────────
function Lights() {
  const zones: Vec3[] = [
    [-1.5, 3, 2.5], // living
    [6.5, 3, 4], // kitchen
    [6.5, 3, -4.5], // dining
    [-5.5, 3, -4.8], // master
    [-8, 3, 5], // foyer
  ]
  return (
    <group>
      <ambientLight intensity={0.18} color="#9fb4ff" />
      <hemisphereLight intensity={0.25} color="#bcd2ff" groundColor="#0a0a12" />
      {/* moon / city fill from outside the south glass */}
      <directionalLight position={[6, 10, 16]} intensity={0.5} color="#cfe0ff" />
      {zones.map((p, i) => (
        <spotLight
          key={i}
          position={[p[0], p[1], p[2]]}
          angle={0.7}
          penumbra={0.8}
          intensity={i === 0 ? 22 : 14}
          distance={12}
          color="#ffd9a0"
          castShadow={i < 2}
          shadow-mapSize-width={1024}
          shadow-mapSize-height={1024}
          shadow-bias={-0.0005}
          target-position={[p[0], 0, p[2]]}
        />
      ))}
    </group>
  )
}

// ─── Full scene ──────────────────────────────────────────────────────────────
export function Penthouse() {
  return (
    <group>
      <Lights />
      <Floor />
      <Shell />
      <Partitions />
      <LivingCluster />
      <KitchenCluster />
      <DiningCluster />
      <BedroomCluster />
      <Terrace />
      <Skyline />
      <Stars radius={120} depth={60} count={2500} factor={5} saturation={0} fade speed={0.5} />
      <Sparkles count={40} scale={[FOOTPRINT.width, CEILING_H, FOOTPRINT.depth]} size={2} speed={0.3} opacity={0.4} color="#cfe0ff" />
      <ContactShadows position={[0, 0.02, 0]} opacity={0.55} scale={26} blur={2.4} far={6} resolution={1024} color="#000000" />
    </group>
  )
}
