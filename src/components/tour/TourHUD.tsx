'use client'

import { motion, AnimatePresence } from 'framer-motion'
import { Minimap } from './Minimap'
import { type Waypoint, type Vec2 } from './tourData'

type Mode = 'explore' | 'dollhouse' | 'fpv'

interface HUDProps {
  mode: Mode
  view: string
  activeId: string | null
  activeWaypoint: Waypoint | null
  currentPos: Vec2
  guideActive: boolean
  showPlan: boolean
  waypoints: Waypoint[]
  onExplore: () => void
  onDollhouse: () => void
  onTogglePlan: () => void
  onToggleGuide: () => void
  onNavigate: (id: string) => void
  onNext: () => void
  onPrev: () => void
}

const ease = [0.16, 1, 0.3, 1] as const

function ModeButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`whitespace-nowrap rounded-xl px-3 py-2 text-[12px] font-semibold tracking-wide transition-all duration-300 md:px-4 md:text-[13px] ${
        active
          ? 'bg-[rgba(0,240,255,0.14)] text-[#7df0ff] shadow-[0_0_20px_rgba(0,240,255,0.18)] border border-[rgba(0,240,255,0.35)]'
          : 'text-[rgba(224,232,255,0.65)] hover:text-[#e0e8ff] border border-transparent hover:bg-[rgba(255,255,255,0.05)]'
      }`}
    >
      {children}
    </button>
  )
}

export function TourHUD(props: HUDProps) {
  const {
    mode,
    activeId,
    activeWaypoint,
    currentPos,
    guideActive,
    showPlan,
    waypoints,
    onExplore,
    onDollhouse,
    onTogglePlan,
    onToggleGuide,
    onNavigate,
    onNext,
    onPrev,
  } = props

  return (
    <div className="pointer-events-none absolute inset-0 z-20 select-none">
      {/* ── Top bar ─────────────────────────────────────────────── */}
      <div className="absolute top-0 left-0 right-0 flex flex-col items-stretch gap-2 p-3 md:flex-row md:items-start md:justify-between md:p-7">
        <motion.div
          initial={{ opacity: 0, y: -16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, ease }}
          className="glass-panel pointer-events-auto self-start rounded-2xl px-3 py-2 md:px-5 md:py-3"
          data-testid="tour-property-label"
        >
          <div className="flex items-center gap-3">
            <span className="h-2 w-2 rounded-full bg-[#00f0ff] shadow-[0_0_10px_#00f0ff]" />
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.22em] text-[rgba(0,240,255,0.8)]">
                Oracle · Spatial Tour
              </div>
              <div className="text-[15px] font-semibold text-[#e0e8ff]">
                The Aerie Penthouse
              </div>
            </div>
          </div>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: -16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.08, ease }}
          className="glass-panel pointer-events-auto flex items-center justify-center gap-1 rounded-2xl p-1.5 md:gap-1.5"
          data-testid="tour-mode-controls"
        >
          <ModeButton active={mode === 'explore'} onClick={onExplore}>
            Explore
          </ModeButton>
          <ModeButton active={mode === 'dollhouse'} onClick={onDollhouse}>
            Dollhouse
          </ModeButton>
          <ModeButton active={showPlan} onClick={onTogglePlan}>
            Floor Plan
          </ModeButton>
        </motion.div>
      </div>

      {/* ── Room detail card (FPV) ──────────────────────────────── */}
      <AnimatePresence mode="wait">
        {activeWaypoint && mode === 'fpv' && (
          <motion.div
            key={activeWaypoint.id}
            initial={{ opacity: 0, x: 40 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 40 }}
            transition={{ duration: 0.55, ease }}
            className="glass-panel pointer-events-auto absolute right-5 top-1/2 w-[300px] -translate-y-1/2 rounded-2xl p-5 md:right-7"
          >
            <div className="mb-1 text-[10px] font-bold uppercase tracking-[0.18em] text-[#8b5cf6]">
              {activeWaypoint.tag}
            </div>
            <h2 className="mb-3 text-2xl font-semibold text-[#e0e8ff]">
              {activeWaypoint.name}
            </h2>
            <p className="mb-4 text-[13px] leading-relaxed text-[rgba(224,232,255,0.62)]">
              {activeWaypoint.narration}
            </p>
            <div className="grid grid-cols-2 gap-2">
              {activeWaypoint.stats.map((s) => (
                <div
                  key={s.label}
                  className="rounded-xl border border-[rgba(255,255,255,0.06)] bg-[rgba(255,255,255,0.03)] px-3 py-2"
                >
                  <div className="text-[9px] font-semibold uppercase tracking-wider text-[rgba(0,240,255,0.7)]">
                    {s.label}
                  </div>
                  <div className="text-[12.5px] font-medium text-[#e0e8ff]">
                    {s.value}
                  </div>
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* ── Minimap (bottom-left) ───────────────────────────────── */}
      <motion.div
        initial={{ opacity: 0, y: 24 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.7, delay: 0.16, ease }}
        className="glass-panel pointer-events-auto absolute bottom-20 left-3 w-[164px] rounded-2xl p-2 md:bottom-7 md:left-7 md:w-[228px] md:p-3"
        data-testid="tour-minimap"
      >
        <div className="mb-2 flex items-center justify-between px-1">
          <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-[rgba(224,232,255,0.5)]">
            Floor 42 · Plan
          </span>
          <span className="text-[10px] font-medium text-[rgba(0,240,255,0.75)]">
            {activeWaypoint ? activeWaypoint.name : 'Overview'}
          </span>
        </div>
        <Minimap
          activeId={activeId}
          currentPos={currentPos}
          onNavigate={onNavigate}
          waypoints={waypoints}
          gradientId="posGlow-sm"
        />
      </motion.div>

      {/* ── AI guide bar (bottom-center) ────────────────────────── */}
      <motion.div
        initial={{ opacity: 0, y: 24 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.7, delay: 0.22, ease }}
        className="glass-panel pointer-events-auto absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1 rounded-2xl p-1.5 md:bottom-7 md:gap-2 md:p-2"
        data-testid="tour-guide-controls"
      >
        <button
          type="button"
          onClick={onPrev}
          aria-label="Previous room"
          className="grid h-10 w-10 place-items-center rounded-xl text-[#e0e8ff] transition hover:bg-[rgba(255,255,255,0.06)]"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M15 18l-6-6 6-6" />
          </svg>
        </button>

        <button
          type="button"
          onClick={onToggleGuide}
          aria-pressed={guideActive}
          className={`flex items-center gap-2.5 rounded-xl px-4 py-2.5 text-[13px] font-semibold transition-all duration-300 ${
            guideActive
              ? 'bg-[rgba(139,92,246,0.18)] text-[#c4b5fd] border border-[rgba(139,92,246,0.4)] shadow-[0_0_24px_rgba(139,92,246,0.22)]'
              : 'bg-[rgba(0,240,255,0.12)] text-[#7df0ff] border border-[rgba(0,240,255,0.3)]'
          }`}
        >
          {guideActive ? (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
              <rect x="6" y="5" width="4" height="14" rx="1" />
              <rect x="14" y="5" width="4" height="14" rx="1" />
            </svg>
          ) : (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
              <path d="M8 5v14l11-7z" />
            </svg>
          )}
          {guideActive ? 'AI Guide · Touring' : 'Start AI Tour'}
        </button>

        <button
          type="button"
          onClick={onNext}
          aria-label="Next room"
          className="grid h-10 w-10 place-items-center rounded-xl text-[#e0e8ff] transition hover:bg-[rgba(255,255,255,0.06)]"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M9 18l6-6-6-6" />
          </svg>
        </button>
      </motion.div>

      {/* ── Floor-plan overlay ──────────────────────────────────── */}
      <AnimatePresence>
        {showPlan && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.4 }}
            className="pointer-events-auto absolute inset-0 z-30 grid place-items-center bg-[rgba(2,4,8,0.74)] p-3 backdrop-blur-sm"
            onClick={onTogglePlan}
          >
            <motion.div
              initial={{ scale: 0.92, y: 20, opacity: 0 }}
              animate={{ scale: 1, y: 0, opacity: 1 }}
              exit={{ scale: 0.92, y: 20, opacity: 0 }}
              transition={{ duration: 0.45, ease }}
              className="glass-panel w-full max-w-[516px] rounded-3xl p-4 md:p-7"
              role="dialog"
              aria-modal="true"
              aria-label="Interactive property floor plan"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="mb-4 flex items-center justify-between">
                <div>
                  <div className="text-[11px] font-semibold uppercase tracking-[0.2em] text-[rgba(0,240,255,0.8)]">
                    Floor 42 · Interactive Plan
                  </div>
                  <div className="text-base font-semibold text-[#e0e8ff] md:text-xl">
                    The Aerie Penthouse — 318 m²
                  </div>
                </div>
                <button
                  type="button"
                  onClick={onTogglePlan}
                  aria-label="Close floor plan"
                  className="grid h-9 w-9 place-items-center rounded-xl text-[#e0e8ff] transition hover:bg-[rgba(255,255,255,0.08)]"
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M18 6L6 18M6 6l12 12" />
                  </svg>
                </button>
              </div>
              <Minimap
                large
                activeId={activeId}
                currentPos={currentPos}
                waypoints={waypoints}
                gradientId="posGlow-lg"
                onNavigate={(id) => {
                  onNavigate(id)
                  onTogglePlan()
                }}
              />
              <div className="mt-4 flex flex-wrap gap-2">
                {waypoints.map((wp) => (
                  <button
                    key={wp.id}
                    type="button"
                    onClick={() => {
                      onNavigate(wp.id)
                      onTogglePlan()
                    }}
                    className="rounded-lg border border-[rgba(255,255,255,0.08)] bg-[rgba(255,255,255,0.03)] px-3 py-1.5 text-[12px] font-medium text-[rgba(224,232,255,0.75)] transition hover:border-[rgba(0,240,255,0.4)] hover:text-[#7df0ff]"
                  >
                    {wp.name}
                  </button>
                ))}
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
