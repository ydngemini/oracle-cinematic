'use client'

import { useRef, useEffect, useState } from 'react'
import { motion, useInView } from 'framer-motion'
import { GlassPanel } from '@/components/ui/GlassPanel'
import { AnimatedText } from '@/components/ui/AnimatedText'
import { HolographicBadge } from '@/components/ui/HolographicBadge'

interface MetricProps {
  label: string
  value: number
  suffix: string
  delay: number
}

function AnimatedMetric({ label, value, suffix, delay }: MetricProps) {
  const ref = useRef<HTMLDivElement>(null)
  const isInView = useInView(ref, { once: true })
  const [display, setDisplay] = useState(0)

  useEffect(() => {
    if (!isInView) return
    const timeout = setTimeout(() => {
      let start = 0
      const duration = 2000
      const startTime = performance.now()

      function animate(now: number) {
        const elapsed = now - startTime
        const progress = Math.min(elapsed / duration, 1)
        const eased = 1 - Math.pow(1 - progress, 4)
        setDisplay(Math.round(eased * value))
        if (progress < 1) requestAnimationFrame(animate)
      }

      requestAnimationFrame(animate)
    }, delay * 1000)

    return () => clearTimeout(timeout)
  }, [isInView, value, delay])

  return (
    <div ref={ref} className="text-center">
      <div className="text-4xl md:text-5xl font-bold text-holo-white mb-2 font-mono">
        {display.toLocaleString()}
        <span className="text-cyan-glow/60 text-2xl ml-1">{suffix}</span>
      </div>
      <div className="text-sm text-white/30 uppercase tracking-wider">{label}</div>
    </div>
  )
}

function PulseBar({ progress, color, delay }: { progress: number; color: string; delay: number }) {
  return (
    <motion.div
      initial={{ scaleX: 0 }}
      whileInView={{ scaleX: 1 }}
      viewport={{ once: true }}
      transition={{ duration: 1.5, delay, ease: [0.16, 1, 0.3, 1] }}
      className="h-1 rounded-full origin-left"
      style={{
        width: `${progress}%`,
        background: `linear-gradient(90deg, ${color}, transparent)`,
      }}
    />
  )
}

const metrics = [
  { label: 'Properties Analyzed', value: 147832, suffix: '+' },
  { label: 'Agent Decisions/sec', value: 10400, suffix: '/s' },
  { label: 'System Uptime', value: 99, suffix: '.97%' },
  { label: 'Neural Pathways', value: 2400, suffix: 'M' },
]

const systemBars = [
  { label: 'CPU Utilization', progress: 34, color: '#00f0ff' },
  { label: 'Memory Allocation', progress: 67, color: '#4060ff' },
  { label: 'GPU Compute', progress: 89, color: '#8b5cf6' },
  { label: 'Network I/O', progress: 23, color: '#ff3366' },
]

export function MetricsSection() {
  return (
    <section className="relative py-32 px-6 md:px-12">
      <div className="absolute inset-0 bg-gradient-to-b from-void via-obsidian/30 to-void" />

      <div className="relative max-w-7xl mx-auto">
        <div className="text-center mb-20">
          <HolographicBadge text="System Metrics" className="mb-6" />
          <AnimatedText
            text="Performance at Scale"
            as="h2"
            className="justify-center text-4xl md:text-6xl font-bold tracking-tight text-holo-white"
          />
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-8 mb-16">
          {metrics.map((m, i) => (
            <GlassPanel key={m.label} glow="cyan" hover={false} delay={i * 0.1}>
              <AnimatedMetric {...m} delay={i * 0.2} />
            </GlassPanel>
          ))}
        </div>

        <GlassPanel glow="none" hover={false} className="max-w-3xl mx-auto">
          <h3 className="text-sm font-mono text-white/40 uppercase tracking-wider mb-6">
            System Resources
          </h3>
          <div className="space-y-5">
            {systemBars.map((bar, i) => (
              <div key={bar.label}>
                <div className="flex justify-between text-xs text-white/40 mb-2">
                  <span>{bar.label}</span>
                  <span className="font-mono">{bar.progress}%</span>
                </div>
                <div className="h-1 bg-white/[0.04] rounded-full overflow-hidden">
                  <PulseBar progress={bar.progress} color={bar.color} delay={i * 0.15} />
                </div>
              </div>
            ))}
          </div>
        </GlassPanel>
      </div>
    </section>
  )
}
