'use client'

import { useEffect, useRef } from 'react'
import { useThree, useFrame } from '@react-three/fiber'
import { OrbitControls } from '@react-three/drei'
import * as THREE from 'three'
import type { CameraPose } from './tourData'

type Mode = 'explore' | 'dollhouse' | 'fpv'

const easeInOutCubic = (t: number) =>
  t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2

export function CameraRig({ pose, mode }: { pose: CameraPose; mode: Mode }) {
  const camera = useThree((s) => s.camera)
  // drei's OrbitControls ref resolves to the three-stdlib impl; typed loosely on purpose.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const controls = useRef<any>(null)

  const anim = useRef({
    from: new THREE.Vector3(),
    to: new THREE.Vector3(),
    fromT: new THREE.Vector3(),
    toT: new THREE.Vector3(),
    t: 1,
    active: false,
    duration: 1.4,
  })

  // Kick off a glide whenever the requested pose changes.
  useEffect(() => {
    const a = anim.current
    a.from.copy(camera.position)
    a.to.set(...pose.position)
    if (controls.current) a.fromT.copy(controls.current.target)
    else a.fromT.set(0, 1, 0)
    a.toT.set(...pose.target)
    a.t = 0
    a.active = true
    a.duration = mode === 'dollhouse' ? 1.6 : 1.3
  }, [pose, mode, camera])

  useFrame((_, delta) => {
    const a = anim.current
    const c = controls.current
    if (a.active) {
      a.t = Math.min(1, a.t + delta / a.duration)
      const e = easeInOutCubic(a.t)
      camera.position.lerpVectors(a.from, a.to, e)
      if (c) {
        c.target.lerpVectors(a.fromT, a.toT, e)
        c.update()
      } else {
        camera.lookAt(a.toT)
      }
      if (a.t >= 1) a.active = false
    } else if (c) {
      c.update()
    }
  })

  const constraints =
    mode === 'dollhouse'
      ? { minDistance: 12, maxDistance: 36, maxPolarAngle: Math.PI / 2.05 }
      : mode === 'fpv'
        ? { minDistance: 1.5, maxDistance: 7, maxPolarAngle: Math.PI / 1.9 }
        : { minDistance: 6, maxDistance: 30, maxPolarAngle: Math.PI / 2.05 }

  return (
    <OrbitControls
      ref={controls}
      makeDefault
      enablePan={false}
      enableZoom
      enableRotate={!anim.current.active}
      enableDamping
      dampingFactor={0.08}
      rotateSpeed={mode === 'fpv' ? 0.5 : 0.6}
      zoomSpeed={0.7}
      minDistance={constraints.minDistance}
      maxDistance={constraints.maxDistance}
      minPolarAngle={0.15}
      maxPolarAngle={constraints.maxPolarAngle}
    />
  )
}
