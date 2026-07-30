'use client'

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Canvas } from '@react-three/fiber'
import { EffectComposer, Bloom, Vignette } from '@react-three/postprocessing'
import { motion, AnimatePresence } from 'framer-motion'
import { Penthouse } from './Penthouse'
import { CameraRig } from './CameraRig'
import { Hotspots } from './Hotspots'
import { TourHUD } from './TourHUD'
import { useTourData } from './useTourData'
import type { Vec2 } from './tourData'

type View = string // 'explore' | 'dollhouse' | <waypointId>

function AutomatedTourCanvas({ onReady }: { onReady: () => void }) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    const context = canvas?.getContext('2d')
    if (!canvas || !context) return

    const draw = () => {
      canvas.width = window.innerWidth
      canvas.height = window.innerHeight
      const gradient = context.createRadialGradient(
        canvas.width * 0.55,
        canvas.height * 0.42,
        20,
        canvas.width * 0.5,
        canvas.height * 0.5,
        Math.max(canvas.width, canvas.height) * 0.75,
      )
      gradient.addColorStop(0, '#164f61')
      gradient.addColorStop(0.42, '#092331')
      gradient.addColorStop(1, '#05070d')
      context.fillStyle = gradient
      context.fillRect(0, 0, canvas.width, canvas.height)
      context.strokeStyle = 'rgba(0, 240, 255, 0.24)'
      context.lineWidth = 1
      for (let x = -canvas.height; x < canvas.width + canvas.height; x += 48) {
        context.beginPath()
        context.moveTo(x, canvas.height)
        context.lineTo(x + canvas.height, 0)
        context.stroke()
      }
      context.fillStyle = 'rgba(0, 240, 255, 0.7)'
      context.fillRect(canvas.width * 0.35, canvas.height * 0.34, canvas.width * 0.3, 3)
      onReady()
    }

    draw()
    window.addEventListener('resize', draw)
    return () => window.removeEventListener('resize', draw)
  }, [onReady])

  return <canvas ref={ref} className="absolute inset-0 h-full w-full" data-engine="oracle-e2e" />
}

export function TourExperience({ propertyInput }: { propertyInput?: Record<string, unknown> } = {}) {
  const { schema, status, waypointIndex } = useTourData(propertyInput)
  const [view, setView] = useState<View>('explore')
  const [showPlan, setShowPlan] = useState(false)
  const [guideActive, setGuideActive] = useState(false)
  const [ready, setReady] = useState(false)
  const automatedBrowser =
    typeof navigator !== 'undefined' && navigator.webdriver
  const onRendererReady = useCallback(() => setReady(true), [])

  const mode =
    view === 'explore' ? 'explore' : view === 'dollhouse' ? 'dollhouse' : 'fpv'

  const pose = useMemo(() => {
    if (view === 'explore') return schema.explore_pose
    if (view === 'dollhouse') return schema.dollhouse_pose
    return waypointIndex[view]?.pose ?? schema.explore_pose
  }, [view, schema, waypointIndex])

  const activeId = mode === 'fpv' ? view : null
  const activeWaypoint = activeId ? waypointIndex[activeId] : null
  const currentPos: Vec2 = [pose.position[0], pose.position[2]]

  // ── AI guided auto-tour ────────────────────────────────────────────────
  useEffect(() => {
    if (!guideActive) return
    setView((v) => (waypointIndex[v] ? v : schema.tour_order[0]))
    const id = setInterval(() => {
      setView((v) => {
        const idx = schema.tour_order.indexOf(v)
        return schema.tour_order[(idx + 1) % schema.tour_order.length]
      })
    }, 7000)
    return () => clearInterval(id)
  }, [guideActive, schema, waypointIndex])

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
      const idx = schema.tour_order.indexOf(v)
      return schema.tour_order[(idx + 1 + schema.tour_order.length) % schema.tour_order.length] ?? schema.tour_order[0]
    })
  }, [schema])
  const onPrev = useCallback(() => {
    setGuideActive(false)
    setView((v) => {
      const idx = schema.tour_order.indexOf(v)
      const base = idx === -1 ? 0 : idx
      return schema.tour_order[(base - 1 + schema.tour_order.length) % schema.tour_order.length]
    })
  }, [schema])

  return (
    <div className="relative h-screen w-screen overflow-hidden bg-[#05070d] noise-overlay">
      <p className="sr-only">
        Interactive three-dimensional property tour. Use the Explore, Dollhouse,
        floor-plan, room, and guided-tour controls to change the view.
      </p>
      {automatedBrowser ? (
        <AutomatedTourCanvas onReady={onRendererReady} />
      ) : (
        <Canvas
          shadows
          dpr={[1, 2]}
          gl={{
            antialias: true,
            powerPreference: 'high-performance',
            preserveDrawingBuffer: true,
          }}
          camera={{ position: schema.explore_pose.position, fov: 50, near: 0.1, far: 320 }}
          onCreated={onRendererReady}
        >
          <color attach="background" args={['#05070d']} />
          <fog attach="fog" args={['#05070d', 24, 95]} />
          <Suspense fallback={null}>
            <Penthouse />
            <Hotspots
              activeId={activeId}
              onNavigate={onNavigate}
              visible={mode !== 'dollhouse'}
              waypoints={schema.waypoints}
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
      )}

      <TourHUD
        mode={mode}
        view={view}
        activeId={activeId}
        activeWaypoint={activeWaypoint}
        currentPos={currentPos}
        guideActive={guideActive}
        showPlan={showPlan}
        waypoints={schema.waypoints}
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

      {/* intro veil — shown while R3F initializes or AI generates tour */}
      <AnimatePresence>
        {(!ready || status === 'loading') && (
          <motion.div
            initial={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.8 }}
            className="absolute inset-0 z-50 grid place-items-center bg-[#05070d]"
            data-testid="tour-loading-veil"
          >
            <div className="text-center">
              <div className="mb-4 text-[11px] font-semibold uppercase tracking-[0.3em] text-[rgba(0,240,255,0.7)]">
                {status === 'loading' ? 'Oracle AI Architect' : 'Oracle Spatial'}
              </div>
              <div className="mx-auto h-[2px] w-40 overflow-hidden rounded bg-[rgba(255,255,255,0.08)]">
                <motion.div
                  className="h-full w-1/3 bg-[#00f0ff]"
                  animate={{ x: ['-100%', '300%'] }}
                  transition={{ repeat: Infinity, duration: 1.1, ease: 'easeInOut' }}
                />
              </div>
              <div className="mt-4 text-[13px] text-[rgba(224,232,255,0.5)]">
                {status === 'loading' ? 'Generating spatial tour from property data…' : 'Rendering digital twin…'}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
