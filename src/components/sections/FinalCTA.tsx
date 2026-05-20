'use client'

import { motion } from 'framer-motion'
import dynamic from 'next/dynamic'
import { AnimatedText } from '@/components/ui/AnimatedText'
import { MagneticButton } from '@/components/ui/MagneticButton'

const CTAScene = dynamic(
  () => import('@/components/three/CTAScene').then((m) => ({ default: m.CTAScene })),
  { ssr: false }
)

export function FinalCTA() {
  return (
    <section className="relative py-32 px-6 md:px-12 overflow-hidden min-h-[80vh] flex items-center justify-center">
      <div className="absolute inset-0">
        <CTAScene />
      </div>

      <div className="absolute inset-0 bg-gradient-to-t from-void via-void/50 to-transparent" />
      <div className="absolute inset-0 bg-gradient-to-b from-void/80 via-transparent to-void" />

      <motion.div
        initial={{ opacity: 0, y: 50 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true }}
        transition={{ duration: 1.2, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 text-center max-w-4xl mx-auto"
      >
        <AnimatedText
          text="The Future of Real Estate is Autonomous"
          as="h2"
          className="justify-center text-4xl md:text-6xl lg:text-7xl font-bold tracking-tight text-holo-white mb-8"
        />

        <motion.p
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true }}
          transition={{ delay: 0.6 }}
          className="text-white/40 text-lg md:text-xl max-w-2xl mx-auto mb-12"
        >
          Deploy Oracle&apos;s multi-agent intelligence system and transform your
          portfolio into a self-optimizing machine.
        </motion.p>

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ delay: 0.8 }}
          className="flex gap-4 justify-center flex-wrap"
        >
          <MagneticButton variant="primary">Deploy Oracle</MagneticButton>
          <MagneticButton variant="ghost">Request Access</MagneticButton>
        </motion.div>

        <motion.div
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true }}
          transition={{ delay: 1.2 }}
          className="mt-16 flex items-center justify-center gap-8 text-xs font-mono text-white/20 uppercase tracking-wider"
        >
          <span>SOC 2 Compliant</span>
          <span className="w-1 h-1 rounded-full bg-white/20" />
          <span>Enterprise Ready</span>
          <span className="w-1 h-1 rounded-full bg-white/20" />
          <span>99.97% Uptime</span>
        </motion.div>
      </motion.div>
    </section>
  )
}
