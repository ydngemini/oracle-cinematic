'use client'

import { Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { Canvas } from '@react-three/fiber'
import { EffectComposer, Bloom, Vignette } from '@react-three/postprocessing'
import { motion, AnimatePresence } from 'framer-motion'
import { Penthouse } from './Penthouse'
import { CameraRig } from './CameraRig'
import { Hotspots } from './Hotspots'
import { TourHUD } from './TourHUD'
import {
  WAYPOINT_INDEX,
  TOUR_ORDER,
  EXPLORE_POSE,
  DOLLHOUSE_POSE,
  type Vec2,
} from './tourData'

type View = string // 'explore' | 'dollhouse' | <waypointId>

export function TourExperience() {
  const [view, setView] = useState<View>('explore')
  const [showPlan, setShowPlan] = useState(false)
  const [guideActive, setGuideActive] = useState(false)
  const [ready, setReady] = useState(false)

  const mode =
    view === 'explore' ? 'explore' : view === 'dollhouse' ? 'dollhouse' : 'fpv'

  const pose = useMemo(() => {
    if (view === 'explore') return EXPLORE_POSE
    if (view === 'dollhouse') return DOLLHOUSE_POSE
    return WAYPOINT_INDEX[view]?.pose ?? EXPLORE_POSE
  }, [view])

  const activeId = mode === 'fpv' ? view : null
  const activeWaypoint = activeId ? WAYPOINT_INDEX[activeId] : null
  const currentPos: Vec2 = [pose.position[0], pose.position[2]]

  // ── AI guided auto-tour ────────────────────────────────────────────────
  useEffect(() => {
    if (!guideActive) return
    setView((v) => (WAYPOINT_INDEX[v] ? v : TOUR_ORDER[0]))
    const id = setInterval(() => {
      setView((v) => {
        const idx = TOUR_ORDER.indexOf(v)
        return TOUR_ORDER[(idx + 1) % TOUR_ORDER.length]
      })
    }, 7000)
    return () => clearInterval(id)
  }, [guideActive])

  // ── Handlers ───────────────────────────────────────────────────────────
  const onExplore = useCallback(() => {
    setGuideActive(false)
    setShowPlan(false)
    setView('explore')
  }, [])
  const onDollhouse = useCallback(() => {
    setGuideActive(false)
    setShowPlan(false)
    setView('dollhouse')
  }, [])
  const onTogglePlan = useCallback(() => setShowPlan((p) => !p), [])
  const onToggleGuide = useCallback(() => setGuideActive((g) => !g), [])
  const onNavigate = useCallback((id: string) => {
    setGuideActive(false)
    setView(id)
  }, [])
  const onNext = useCallback(() => {
    setGuideActive(false)
    setView((v) => {
      const idx = TOUR_ORDER.indexOf(v)
      return TOUR_ORDER[(idx + 1 + TOUR_ORDER.length) % TOUR_ORDER.length] ?? TOUR_ORDER[0]
    })
  }, [])
  const onPrev = useCallback(() => {
    setGuideActive(false)
    setView((v) => {
      const idx = TOUR_ORDER.indexOf(v)
      const base = idx === -1 ? 0 : idx
      return TOUR_ORDER[(base - 1 + TOUR_ORDER.length) % TOUR_ORDER.length]
    })
  }, [])

  return (
    <div className="relative h-screen w-screen overflow-hidden bg-[#05070d] noise-overlay">
      <Canvas
        shadows
        dpr={[1, 2]}
        gl={{ antialias: true, powerPreference: 'high-performance' }}
        camera={{ position: EXPLORE_POSE.position, fov: 50, near: 0.1, far: 320 }}
        onCreated={() => setReady(true)}
      >
        <color attach="background" args={['#05070d']} />
        <fog attach="fog" args={['#05070d', 24, 95]} />
        <Suspense fallback={null}>
          <Penthouse />
          <Hotspots
            activeId={activeId}
            onNavigate={onNavigate}
            visible={mode !== 'dollhouse'}
          />
        </Suspense>
        <CameraRig pose={pose} mode={mode} />
        <EffectComposer>
          <Bloom
            intensity={0.85}
            luminanceThreshold={0.5}
            luminanceSmoothing={0.32}
            mipmapBlur
          />
          <Vignette eskil={false} offset={0.22} darkness={0.82} />
        </EffectComposer>
      </Canvas>

      <TourHUD
        mode={mode}
        view={view}
        activeId={activeId}
        activeWaypoint={activeWaypoint}
        currentPos={currentPos}
        guideActive={guideActive}
        showPlan={showPlan}
        onExplore={onExplore}
        onDollhouse={onDollhouse}
        onTogglePlan={onTogglePlan}
        onToggleGuide={onToggleGuide}
        onNavigate={onNavigate}
        onNext={onNext}
        onPrev={onPrev}
      />

      {/* subtle drag hint */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: ready ? 1 : 0 }}
        transition={{ delay: 1.2, duration: 1 }}
        className="pointer-events-none absolute bottom-7 right-7 z-20 hidden text-[11px] font-medium tracking-wide text-[rgba(224,232,255,0.4)] md:block"
      >
        Drag to orbit · Scroll to zoom · Click a point to move
      </motion.div>

      {/* intro veil */}
      <AnimatePresence>
        {!ready && (
          <motion.div
            initial={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.8 }}
            className="absolute inset-0 z-50 grid place-items-center bg-[#05070d]"
          >
            <div className="text-center">
              <div className="mb-4 text-[11px] font-semibold uppercase tracking-[0.3em] text-[rgba(0,240,255,0.7)]">
                Oracle Spatial
              </div>
              <div className="mx-auto h-[2px] w-40 overflow-hidden rounded bg-[rgba(255,255,255,0.08)]">
                <motion.div
                  className="h-full w-1/3 bg-[#00f0ff]"
                  animate={{ x: ['-100%', '300%'] }}
                  transition={{ repeat: Infinity, duration: 1.1, ease: 'easeInOut' }}
                />
              </div>
              <div className="mt-4 text-[13px] text-[rgba(224,232,255,0.5)]">
                Rendering digital twin…
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
