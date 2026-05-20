'use client'

import { motion } from 'framer-motion'
import { clsx } from 'clsx'

interface GlassPanelProps {
  children: React.ReactNode
  className?: string
  glow?: 'cyan' | 'violet' | 'crimson' | 'none'
  hover?: boolean
  delay?: number
}

export function GlassPanel({ children, className, glow = 'none', hover = true, delay = 0 }: GlassPanelProps) {
  const glowStyles = {
    cyan: 'shadow-[0_0_30px_rgba(0,240,255,0.1)] hover:shadow-[0_0_50px_rgba(0,240,255,0.2)]',
    violet: 'shadow-[0_0_30px_rgba(139,92,246,0.1)] hover:shadow-[0_0_50px_rgba(139,92,246,0.2)]',
    crimson: 'shadow-[0_0_30px_rgba(255,51,102,0.1)] hover:shadow-[0_0_50px_rgba(255,51,102,0.2)]',
    none: '',
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 30 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: '-50px' }}
      transition={{ duration: 0.8, delay, ease: [0.16, 1, 0.3, 1] }}
      whileHover={hover ? { scale: 1.02, y: -5 } : undefined}
      className={clsx(
        'glass-panel rounded-2xl p-6 transition-shadow duration-500',
        glowStyles[glow],
        className
      )}
    >
      {children}
    </motion.div>
  )
}
