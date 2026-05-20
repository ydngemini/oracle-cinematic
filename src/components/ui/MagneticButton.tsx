'use client'

import { useRef, useState } from 'react'
import { motion } from 'framer-motion'
import { clsx } from 'clsx'

interface MagneticButtonProps {
  children: React.ReactNode
  className?: string
  variant?: 'primary' | 'ghost'
  onClick?: () => void
}

export function MagneticButton({ children, className, variant = 'primary', onClick }: MagneticButtonProps) {
  const ref = useRef<HTMLButtonElement>(null)
  const [position, setPosition] = useState({ x: 0, y: 0 })

  function handleMouse(e: React.MouseEvent<HTMLButtonElement>) {
    const { clientX, clientY } = e
    const { left, top, width, height } = ref.current!.getBoundingClientRect()
    const x = (clientX - left - width / 2) * 0.3
    const y = (clientY - top - height / 2) * 0.3
    setPosition({ x, y })
  }

  function handleLeave() {
    setPosition({ x: 0, y: 0 })
  }

  const variants = {
    primary: 'bg-gradient-to-r from-cyan-glow/20 to-blue-neon/20 border border-cyan-glow/30 text-cyan-glow hover:border-cyan-glow/60',
    ghost: 'bg-transparent border border-white/10 text-white/80 hover:border-white/30 hover:text-white',
  }

  return (
    <motion.button
      ref={ref}
      onMouseMove={handleMouse}
      onMouseLeave={handleLeave}
      onClick={onClick}
      animate={{ x: position.x, y: position.y }}
      transition={{ type: 'spring', stiffness: 150, damping: 15, mass: 0.1 }}
      whileTap={{ scale: 0.95 }}
      className={clsx(
        'relative px-8 py-4 rounded-xl font-medium text-sm tracking-wider uppercase',
        'transition-all duration-300 cursor-pointer',
        'shadow-[0_0_20px_rgba(0,240,255,0.1)]',
        'hover:shadow-[0_0_40px_rgba(0,240,255,0.3)]',
        variants[variant],
        className
      )}
    >
      <span className="relative z-10">{children}</span>
      <motion.div
        className="absolute inset-0 rounded-xl opacity-0 hover:opacity-100 transition-opacity duration-300"
        style={{
          background: 'radial-gradient(circle at center, rgba(0, 240, 255, 0.1), transparent 70%)',
        }}
      />
    </motion.button>
  )
}
