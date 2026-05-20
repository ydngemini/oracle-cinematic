'use client'

import { motion } from 'framer-motion'
import { GlowCard } from '@/components/ui/GlowCard'
import { AnimatedText } from '@/components/ui/AnimatedText'
import { HolographicBadge } from '@/components/ui/HolographicBadge'

const capabilities = [
  {
    title: 'Scouting Matrix',
    description: 'Autonomous property discovery through multi-agent spatial intelligence and real-time market analysis.',
    icon: '◈',
    metric: '10K+ properties/sec',
  },
  {
    title: 'Spatial Staging',
    description: 'AI-driven virtual staging with photorealistic rendering and quantum material simulation.',
    icon: '⬡',
    metric: '4K resolution',
  },
  {
    title: 'Voice Negotiation',
    description: 'Natural language processing engine for autonomous deal negotiation with adaptive strategy.',
    icon: '◉',
    metric: '99.7% accuracy',
  },
  {
    title: 'Legal Intelligence',
    description: 'Automated contract generation and compliance verification with regulatory awareness.',
    icon: '⬢',
    metric: '50 jurisdictions',
  },
]

export function CapabilitiesSection() {
  return (
    <section className="relative py-32 px-6 md:px-12">
      <div className="max-w-7xl mx-auto">
        <div className="text-center mb-20">
          <HolographicBadge text="Core Capabilities" pulse={false} className="mb-6" />
          <AnimatedText
            text="Intelligence Beyond Human Scale"
            as="h2"
            className="justify-center text-4xl md:text-6xl font-bold tracking-tight text-holo-white"
          />
          <motion.p
            initial={{ opacity: 0 }}
            whileInView={{ opacity: 1 }}
            viewport={{ once: true }}
            transition={{ delay: 0.5 }}
            className="mt-6 text-white/40 text-lg max-w-2xl mx-auto"
          >
            Four autonomous agents operating in concert, each specialized for a critical
            dimension of real-estate intelligence.
          </motion.p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {capabilities.map((cap, i) => (
            <GlowCard key={cap.title} delay={i * 0.15}>
              <div className="flex items-start gap-4">
                <span className="text-3xl text-cyan-glow/60">{cap.icon}</span>
                <div className="flex-1">
                  <h3 className="text-xl font-semibold text-holo-white mb-2">{cap.title}</h3>
                  <p className="text-white/40 text-sm leading-relaxed mb-4">{cap.description}</p>
                  <div className="inline-flex items-center gap-2 px-3 py-1 rounded-md bg-cyan-glow/5 border border-cyan-glow/10">
                    <span className="w-1.5 h-1.5 rounded-full bg-cyan-glow animate-pulse" />
                    <span className="text-xs font-mono text-cyan-glow/70">{cap.metric}</span>
                  </div>
                </div>
              </div>
            </GlowCard>
          ))}
        </div>
      </div>
    </section>
  )
}
