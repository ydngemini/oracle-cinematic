'use client'

import { useState, useEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import { AnimatedText } from '@/components/ui/AnimatedText'
import { HolographicBadge } from '@/components/ui/HolographicBadge'

const terminalLines = [
  { type: 'system', text: '[ORACLE v4.2.1] Initializing autonomous agent swarm...' },
  { type: 'info', text: '→ Connecting to Nexus Bus (ws://localhost:9999)' },
  { type: 'success', text: '✓ SCOUTING_MATRIX agent online' },
  { type: 'success', text: '✓ SPATIAL_STAGING agent online' },
  { type: 'success', text: '✓ VOICE_NEGOTIATION agent online' },
  { type: 'info', text: '→ Loading property graph (2.4M nodes, 18.7M edges)' },
  { type: 'data', text: '  ├─ Market vectors: 47 dimensions' },
  { type: 'data', text: '  ├─ Spatial embeddings: bge-small-en-v1.5' },
  { type: 'data', text: '  └─ Price model: XGBoost ensemble (R²=0.94)' },
  { type: 'success', text: '✓ System nominal. Awaiting instruction.' },
  { type: 'prompt', text: '▌' },
]

const typeColors: Record<string, string> = {
  system: 'text-cyan-glow',
  info: 'text-white/50',
  success: 'text-green-400',
  data: 'text-white/30',
  prompt: 'text-cyan-glow',
}

export function TerminalSection() {
  const [visibleLines, setVisibleLines] = useState(0)
  const [isInView, setIsInView] = useState(false)
  const sectionRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting && !isInView) {
          setIsInView(true)
        }
      },
      { threshold: 0.3 }
    )

    if (sectionRef.current) observer.observe(sectionRef.current)
    return () => observer.disconnect()
  }, [isInView])

  useEffect(() => {
    if (!isInView) return
    const interval = setInterval(() => {
      setVisibleLines((prev) => {
        if (prev >= terminalLines.length) {
          clearInterval(interval)
          return prev
        }
        return prev + 1
      })
    }, 400)
    return () => clearInterval(interval)
  }, [isInView])

  return (
    <section ref={sectionRef} className="relative py-32 px-6 md:px-12">
      <div className="max-w-5xl mx-auto">
        <div className="text-center mb-16">
          <HolographicBadge text="System Console" className="mb-6" />
          <AnimatedText
            text="Live System Interface"
            as="h2"
            className="justify-center text-4xl md:text-6xl font-bold tracking-tight text-holo-white"
          />
        </div>

        <motion.div
          initial={{ opacity: 0, y: 40 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 1, ease: [0.16, 1, 0.3, 1] }}
          className="relative rounded-2xl overflow-hidden border border-white/[0.06]"
        >
          <div className="absolute inset-0 bg-gradient-to-br from-obsidian to-void" />
          <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top,rgba(0,240,255,0.03),transparent_50%)]" />

          <div className="relative">
            <div className="flex items-center gap-2 px-6 py-4 border-b border-white/[0.04]">
              <div className="flex gap-1.5">
                <div className="w-3 h-3 rounded-full bg-red-500/60" />
                <div className="w-3 h-3 rounded-full bg-yellow-500/60" />
                <div className="w-3 h-3 rounded-full bg-green-500/60" />
              </div>
              <span className="ml-4 text-xs font-mono text-white/30">oracle-terminal — autonomous-agent-bus</span>
            </div>

            <div className="p-6 font-mono text-sm space-y-1.5 min-h-[320px]">
              {terminalLines.slice(0, visibleLines).map((line, i) => (
                <motion.div
                  key={i}
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ duration: 0.3 }}
                  className={typeColors[line.type]}
                >
                  {line.text}
                </motion.div>
              ))}
            </div>
          </div>

          <div className="absolute bottom-0 left-0 right-0 h-16 bg-gradient-to-t from-obsidian to-transparent pointer-events-none" />
        </motion.div>
      </div>
    </section>
  )
}
