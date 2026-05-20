'use client'

import { motion } from 'framer-motion'
import { clsx } from 'clsx'

interface HolographicBadgeProps {
  text: string
  className?: string
  pulse?: boolean
}

export function HolographicBadge({ text, className, pulse = true }: HolographicBadgeProps) {
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.8 }}
      whileInView={{ opacity: 1, scale: 1 }}
      viewport={{ once: true }}
      className={clsx(
        'inline-flex items-center gap-2 px-4 py-2 rounded-full',
        'bg-gradient-to-r from-cyan-glow/10 to-violet-energy/10',
        'border border-cyan-glow/20',
        'text-xs font-mono uppercase tracking-[0.2em] text-cyan-glow/80',
        className
      )}
    >
      {pulse && (
        <span className="relative flex h-2 w-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-cyan-glow opacity-75" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-cyan-glow" />
        </span>
      )}
      {text}
    </motion.div>
  )
}
