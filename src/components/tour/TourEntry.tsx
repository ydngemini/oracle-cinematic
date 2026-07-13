'use client'

import { useEffect, useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { CEILING_H, FOOTPRINT, WAYPOINTS } from './tourData'

const EASE = [0.16, 1, 0.3, 1] as const

const container = {
  hidden: {},
  show: { transition: { staggerChildren: 0.09, delayChildren: 0.25 } },
}

const rise = {
  hidden: { opacity: 0, y: 28 },
  show: { opacity: 1, y: 0, transition: { duration: 0.9, ease: EASE } },
}

const hairline = {
  hidden: { scaleX: 0 },
  show: { scaleX: 1, transition: { duration: 1.2, ease: EASE } },
}

export function TourEntry({ onEnter }: { onEnter: () => void }) {
  const [clock, setClock] = useState('--:--:--')

  useEffect(() => {
    const tick = () =>
      setClock(
        new Date().toLocaleTimeString('en-US', {
          hour12: false,
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        })
      )
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [])

  const areaSqm = useMemo(() => FOOTPRINT.width * FOOTPRINT.depth, [])

  return (
    <motion.div
      initial={{ clipPath: 'circle(141% at 50% 86%)' }}
      animate={{ clipPath: 'circle(141% at 50% 86%)' }}
      exit={{
        clipPath: 'circle(0% at 50% 86%)',
        transition: { duration: 1.05, ease: [0.65, 0, 0.35, 1] },
      }}
      className="noise-overlay fixed inset-0 z-[60] flex flex-col overflow-hidden bg-[#05070d] text-[#e0e8ff]"
    >
      {/* ── atmosphere ─────────────────────────────────────────────── */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            'radial-gradient(60% 50% at 82% 8%, rgba(0,240,255,0.07) 0%, transparent 65%),' +
            'radial-gradient(50% 45% at 8% 95%, rgba(139,92,246,0.06) 0%, transparent 60%)',
        }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-[0.05]"
        style={{
          backgroundImage:
            'linear-gradient(rgba(224,232,255,0.5) 1px, transparent 1px),' +
            'linear-gradient(90deg, rgba(224,232,255,0.5) 1px, transparent 1px)',
          backgroundSize: '88px 88px',
        }}
      />

      <motion.div
        variants={container}
        initial="hidden"
        animate="show"
        className="relative z-10 flex h-full flex-col px-6 md:px-12"
      >
        {/* ── top frame ──────────────────────────────────────────────── */}
        <motion.header
          variants={rise}
          className="flex items-baseline justify-between pt-6 pb-4 font-mono text-[10px] uppercase tracking-[0.3em] text-[rgba(224,232,255,0.45)]"
        >
          <span className="text-[rgba(0,240,255,0.75)]">Oracle Spatial</span>
          <span className="hidden md:inline">Private Showing — Dossier Nº 001</span>
          <span suppressHydrationWarning>Local {clock}</span>
        </motion.header>
        <motion.div
          variants={hairline}
          className="h-px w-full origin-left bg-[rgba(224,232,255,0.1)]"
        />

        {/* ── main spread ────────────────────────────────────────────── */}
        <div className="grid flex-1 grid-cols-1 items-center gap-10 py-8 md:grid-cols-12 md:gap-6">
          {/* title block */}
          <div className="md:col-span-7">
            <motion.p
              variants={rise}
              className="mb-6 font-mono text-[10px] uppercase tracking-[0.4em] text-[rgba(0,240,255,0.6)]"
            >
              A residence above the skyline
            </motion.p>
            <motion.h1
              variants={rise}
              className="font-serif text-[clamp(3.2rem,9vw,8rem)] font-light leading-[0.92] tracking-tight"
            >
              <span className="italic text-[rgba(224,232,255,0.92)]">The Nocturne</span>
              <br />
              <span className="text-[rgba(224,232,255,0.7)]">Penthouse.</span>
            </motion.h1>
            <motion.p
              variants={rise}
              className="mt-7 max-w-md text-[14px] leading-relaxed text-[rgba(224,232,255,0.5)]"
            >
              A private elevator opens onto a backlit onyx gallery; beyond it,
              full-height glass holds the city at night. Walk the digital twin —
              every room, every sightline, rendered live.
            </motion.p>

            {/* stat strip */}
            <motion.dl
              variants={rise}
              className="mt-10 flex flex-wrap gap-x-10 gap-y-4 font-mono text-[11px] uppercase tracking-[0.18em]"
            >
              {[
                ['Interior', `${areaSqm} m²`],
                ['Spaces', String(WAYPOINTS.length).padStart(2, '0')],
                ['Ceilings', `${CEILING_H} m`],
                ['Glazing', 'Full-height'],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt className="text-[rgba(224,232,255,0.35)]">{label}</dt>
                  <dd className="mt-1 text-[13px] tracking-[0.1em] text-[rgba(0,240,255,0.85)]">
                    {value}
                  </dd>
                </div>
              ))}
            </motion.dl>
          </div>

          {/* room manifest */}
          <div className="md:col-span-4 md:col-start-9">
            <motion.p
              variants={rise}
              className="mb-3 font-mono text-[10px] uppercase tracking-[0.3em] text-[rgba(224,232,255,0.35)]"
            >
              Index of spaces
            </motion.p>
            <div role="list">
              {WAYPOINTS.map((wp, i) => (
                <motion.div
                  role="listitem"
                  key={wp.id}
                  variants={rise}
                  className="group flex items-baseline justify-between border-t border-[rgba(224,232,255,0.08)] py-3 last:border-b"
                >
                  <span className="flex items-baseline gap-4">
                    <span className="font-mono text-[10px] text-[rgba(0,240,255,0.5)] transition-colors group-hover:text-[#00f0ff]">
                      {String(i + 1).padStart(2, '0')}
                    </span>
                    <span
                      className="font-serif text-[17px] font-light text-[rgba(224,232,255,0.8)] transition-colors group-hover:text-white"
                    >
                      {wp.name}
                    </span>
                  </span>
                  <span className="font-mono text-[9px] uppercase tracking-[0.25em] text-[rgba(224,232,255,0.3)] transition-colors group-hover:text-[rgba(0,240,255,0.7)]">
                    {wp.tag}
                  </span>
                </motion.div>
              ))}
            </div>
          </div>
        </div>

        {/* ── aperture enter ─────────────────────────────────────────── */}
        <div className="flex justify-center pb-10">
          <motion.button
            variants={rise}
            onClick={onEnter}
            aria-label="Enter the 3D tour"
            className="group relative grid h-32 w-32 cursor-pointer place-items-center rounded-full outline-none focus-visible:ring-1 focus-visible:ring-[rgba(0,240,255,0.6)]"
          >
            <span
              aria-hidden
              className="absolute inset-0 rounded-full border border-dashed border-[rgba(0,240,255,0.35)] transition-transform duration-700 ease-out group-hover:scale-110"
              style={{ animation: 'rotate-slow 26s linear infinite' }}
            />
            <span
              aria-hidden
              className="absolute inset-[10px] rounded-full border border-[rgba(224,232,255,0.1)] transition-all duration-500 group-hover:inset-[6px] group-hover:border-[rgba(0,240,255,0.45)]"
            />
            <span
              aria-hidden
              className="absolute h-1.5 w-1.5 -translate-y-9 rounded-full bg-[#00f0ff]"
              style={{ animation: 'glow-pulse 2.4s ease-in-out infinite' }}
            />
            <span className="font-mono text-[10px] uppercase leading-relaxed tracking-[0.35em] text-[rgba(224,232,255,0.85)] transition-colors group-hover:text-white">
              Enter
              <br />
              Tour
            </span>
          </motion.button>
        </div>

        {/* ── bottom frame ───────────────────────────────────────────── */}
        <motion.div
          variants={hairline}
          className="h-px w-full origin-left bg-[rgba(224,232,255,0.1)]"
        />
        <motion.footer
          variants={rise}
          className="flex items-baseline justify-between py-4 font-mono text-[9px] uppercase tracking-[0.3em] text-[rgba(224,232,255,0.3)]"
        >
          <span>Digital twin · rendered live</span>
          <span className="hidden md:inline">Skyline exposure — South</span>
          <span>Oracle © {new Date().getFullYear()}</span>
        </motion.footer>
      </motion.div>
    </motion.div>
  )
}
