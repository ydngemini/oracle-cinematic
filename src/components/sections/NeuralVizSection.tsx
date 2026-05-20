'use client'

import { useRef, useMemo } from 'react'
import { motion } from 'framer-motion'
import dynamic from 'next/dynamic'
import { AnimatedText } from '@/components/ui/AnimatedText'
import { HolographicBadge } from '@/components/ui/HolographicBadge'

const NeuralNetwork = dynamic(
  () => import('@/components/three/NeuralNetwork').then((m) => ({ default: m.NeuralNetwork })),
  { ssr: false }
)

export function NeuralVizSection() {
  return (
    <section className="relative py-32 px-6 md:px-12 overflow-hidden">
      <div className="absolute inset-0 bg-gradient-to-b from-void via-obsidian/50 to-void" />

      <div className="relative max-w-7xl mx-auto">
        <div className="text-center mb-16">
          <HolographicBadge text="Neural Architecture" className="mb-6" />
          <AnimatedText
            text="Multi-Agent Neural Topology"
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
            Visualizing the neural pathways connecting Oracle&apos;s autonomous agent swarm
            in real-time decision space.
          </motion.p>
        </div>

        <motion.div
          initial={{ opacity: 0, scale: 0.9 }}
          whileInView={{ opacity: 1, scale: 1 }}
          viewport={{ once: true }}
          transition={{ duration: 1.2, ease: [0.16, 1, 0.3, 1] }}
          className="relative h-[500px] md:h-[600px] rounded-3xl overflow-hidden border border-white/[0.04]"
        >
          <div className="absolute inset-0 bg-obsidian/60 backdrop-blur-sm" />
          <NeuralNetwork />
        </motion.div>
      </div>
    </section>
  )
}
