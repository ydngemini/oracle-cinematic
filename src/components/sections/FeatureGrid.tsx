'use client'

import { motion } from 'framer-motion'
import { GlowCard } from '@/components/ui/GlowCard'
import { AnimatedText } from '@/components/ui/AnimatedText'
import { HolographicBadge } from '@/components/ui/HolographicBadge'

const features = [
  {
    title: 'Quantum Decision Engine',
    description: 'Qiskit-powered quantum circuits for optimal portfolio selection across superposition states.',
    gradient: 'from-cyan-glow/10 to-blue-neon/5',
  },
  {
    title: 'Nexus Bus Protocol',
    description: 'Real-time WebSocket pub/sub backbone enabling sub-millisecond inter-agent communication.',
    gradient: 'from-violet-energy/10 to-cyan-glow/5',
  },
  {
    title: 'Semantic Embedding Router',
    description: 'BGE-small-en vectorization for intent classification and agent dispatch routing.',
    gradient: 'from-blue-neon/10 to-violet-energy/5',
  },
  {
    title: 'LoRA Voice Synthesis',
    description: 'Fine-tuned language models with personality injection for natural negotiation dialogue.',
    gradient: 'from-crimson-accent/10 to-violet-energy/5',
  },
  {
    title: 'Graph Intelligence',
    description: 'Property knowledge graph with 2.4M nodes powering relationship-aware recommendations.',
    gradient: 'from-cyan-glow/10 to-crimson-accent/5',
  },
  {
    title: 'Adaptive Learning',
    description: 'Continuous model refinement from interaction feedback loops and market signal ingestion.',
    gradient: 'from-blue-neon/10 to-crimson-accent/5',
  },
]

export function FeatureGrid() {
  return (
    <section className="relative py-32 px-6 md:px-12">
      <div className="max-w-7xl mx-auto">
        <div className="text-center mb-20">
          <HolographicBadge text="Architecture" pulse={false} className="mb-6" />
          <AnimatedText
            text="Engineered for Autonomy"
            as="h2"
            className="justify-center text-4xl md:text-6xl font-bold tracking-tight text-holo-white"
          />
          <motion.p
            initial={{ opacity: 0 }}
            whileInView={{ opacity: 1 }}
            viewport={{ once: true }}
            transition={{ delay: 0.4 }}
            className="mt-6 text-white/40 text-lg max-w-2xl mx-auto"
          >
            Every subsystem designed for zero-human-intervention operation
            at production scale.
          </motion.p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {features.map((feature, i) => (
            <GlowCard key={feature.title} delay={i * 0.1}>
              <div className={`absolute inset-0 bg-gradient-to-br ${feature.gradient} opacity-50 rounded-2xl`} />
              <div className="relative">
                <h3 className="text-lg font-semibold text-holo-white mb-3">{feature.title}</h3>
                <p className="text-white/35 text-sm leading-relaxed">{feature.description}</p>
              </div>
            </GlowCard>
          ))}
        </div>
      </div>
    </section>
  )
}
