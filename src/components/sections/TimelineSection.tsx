'use client'

import { useRef } from 'react'
import { motion, useScroll, useTransform } from 'framer-motion'
import { AnimatedText } from '@/components/ui/AnimatedText'
import { HolographicBadge } from '@/components/ui/HolographicBadge'

const milestones = [
  {
    phase: '01',
    title: 'Target Acquisition',
    description: 'Scouting Matrix identifies high-potential properties through spatial analysis and market pattern recognition.',
    status: 'active',
  },
  {
    phase: '02',
    title: 'Spatial Intelligence',
    description: 'Properties are staged in virtual space, rendered with quantum material simulations for buyer preview.',
    status: 'active',
  },
  {
    phase: '03',
    title: 'Negotiation Protocol',
    description: 'Voice agent engages in adaptive dialogue, adjusting strategy based on real-time sentiment analysis.',
    status: 'active',
  },
  {
    phase: '04',
    title: 'Legal Execution',
    description: 'Contract generation, compliance verification, and closing documentation produced autonomously.',
    status: 'pending',
  },
  {
    phase: '05',
    title: 'Portfolio Integration',
    description: 'Completed acquisitions are integrated into the master property graph for ongoing optimization.',
    status: 'pending',
  },
]

export function TimelineSection() {
  const containerRef = useRef<HTMLDivElement>(null)
  const { scrollYProgress } = useScroll({
    target: containerRef,
    offset: ['start end', 'end start'],
  })

  const lineHeight = useTransform(scrollYProgress, [0.1, 0.9], ['0%', '100%'])

  return (
    <section ref={containerRef} className="relative py-32 px-6 md:px-12">
      <div className="absolute inset-0 bg-gradient-to-b from-void via-obsidian/30 to-void" />

      <div className="relative max-w-4xl mx-auto">
        <div className="text-center mb-20">
          <HolographicBadge text="Workflow" className="mb-6" />
          <AnimatedText
            text="Autonomous Pipeline"
            as="h2"
            className="justify-center text-4xl md:text-6xl font-bold tracking-tight text-holo-white"
          />
        </div>

        <div className="relative">
          <div className="absolute left-8 md:left-1/2 top-0 bottom-0 w-px bg-white/[0.06]">
            <motion.div
              className="w-full bg-gradient-to-b from-cyan-glow to-violet-energy"
              style={{ height: lineHeight }}
            />
          </div>

          <div className="space-y-16">
            {milestones.map((milestone, i) => (
              <motion.div
                key={milestone.phase}
                initial={{ opacity: 0, x: i % 2 === 0 ? -50 : 50 }}
                whileInView={{ opacity: 1, x: 0 }}
                viewport={{ once: true, margin: '-100px' }}
                transition={{ duration: 0.8, delay: 0.1, ease: [0.16, 1, 0.3, 1] }}
                className={`relative flex items-center gap-8 ${
                  i % 2 === 0 ? 'md:flex-row' : 'md:flex-row-reverse'
                }`}
              >
                <div className={`flex-1 ${i % 2 === 0 ? 'md:text-right' : 'md:text-left'} pl-20 md:pl-0`}>
                  <span className="text-xs font-mono text-cyan-glow/60 uppercase tracking-wider">
                    Phase {milestone.phase}
                  </span>
                  <h3 className="text-xl font-semibold text-holo-white mt-1 mb-2">
                    {milestone.title}
                  </h3>
                  <p className="text-white/35 text-sm leading-relaxed">
                    {milestone.description}
                  </p>
                </div>

                <div className="absolute left-8 md:left-1/2 -translate-x-1/2 w-4 h-4 rounded-full border-2 border-cyan-glow/40 bg-void z-10">
                  {milestone.status === 'active' && (
                    <div className="absolute inset-1 rounded-full bg-cyan-glow animate-pulse" />
                  )}
                </div>

                <div className="flex-1 hidden md:block" />
              </motion.div>
            ))}
          </div>
        </div>
      </div>
    </section>
  )
}
