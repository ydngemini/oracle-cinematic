'use client'

import { useRef } from 'react'
import { motion, useScroll, useTransform } from 'framer-motion'
import dynamic from 'next/dynamic'
import { useMousePosition } from '@/hooks/useMousePosition'
import { AnimatedText } from '@/components/ui/AnimatedText'
import { MagneticButton } from '@/components/ui/MagneticButton'
import { HolographicBadge } from '@/components/ui/HolographicBadge'

const HeroScene = dynamic(
  () => import('@/components/three/HeroScene').then((m) => ({ default: m.HeroScene })),
  { ssr: false }
)

export function HeroSection() {
  const containerRef = useRef<HTMLDivElement>(null)
  const { normalizedX, normalizedY } = useMousePosition()

  const { scrollYProgress } = useScroll({
    target: containerRef,
    offset: ['start start', 'end start'],
  })

  const opacity = useTransform(scrollYProgress, [0, 0.5], [1, 0])
  const scale = useTransform(scrollYProgress, [0, 0.5], [1, 0.8])
  const y = useTransform(scrollYProgress, [0, 0.5], [0, -100])

  return (
    <section ref={containerRef} className="relative h-[200vh]">
      <div className="sticky top-0 h-screen overflow-hidden">
        <HeroScene mouseX={normalizedX} mouseY={normalizedY} />

        <div className="absolute inset-0 bg-gradient-to-b from-transparent via-transparent to-void/80 pointer-events-none" />

        <motion.div
          style={{ opacity, scale, y }}
          className="absolute inset-0 flex flex-col items-center justify-center z-10 px-6"
        >
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.5, duration: 1 }}
          >
            <HolographicBadge text="Autonomous Intelligence System" />
          </motion.div>

          <div className="mt-8 text-center">
            <AnimatedText
              text="ORACLE"
              as="h1"
              delay={0.8}
              stagger={0.08}
              className="justify-center text-[clamp(4rem,12vw,10rem)] font-bold tracking-[-0.04em] leading-none glow-text"
            />
          </div>

          <motion.p
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 1.5, duration: 1, ease: [0.16, 1, 0.3, 1] }}
            className="mt-6 text-lg md:text-xl text-white/50 max-w-2xl text-center font-light tracking-wide"
          >
            Next-generation autonomous real-estate intelligence.
            Spatial staging. Voice negotiation. Quantum decision matrices.
          </motion.p>

          <motion.div
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 2, duration: 1, ease: [0.16, 1, 0.3, 1] }}
            className="mt-12 flex gap-4 flex-wrap justify-center"
          >
            <MagneticButton variant="primary">Initialize System</MagneticButton>
            <MagneticButton variant="ghost">View Architecture</MagneticButton>
          </motion.div>

          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 3, duration: 2 }}
            className="absolute bottom-12 left-1/2 -translate-x-1/2"
          >
            <motion.div
              animate={{ y: [0, 8, 0] }}
              transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
              className="w-6 h-10 rounded-full border border-white/20 flex items-start justify-center p-2"
            >
              <motion.div className="w-1 h-2 rounded-full bg-cyan-glow/60" />
            </motion.div>
          </motion.div>
        </motion.div>
      </div>
    </section>
  )
}
