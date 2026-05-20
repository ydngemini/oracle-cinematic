'use client'

import { useRef, useState } from 'react'
import { motion } from 'framer-motion'
import { clsx } from 'clsx'

interface GlowCardProps {
  children: React.ReactNode
  className?: string
  delay?: number
}

export function GlowCard({ children, className, delay = 0 }: GlowCardProps) {
  const cardRef = useRef<HTMLDivElement>(null)
  const [rotateX, setRotateX] = useState(0)
  const [rotateY, setRotateY] = useState(0)
  const [glowPosition, setGlowPosition] = useState({ x: 50, y: 50 })

  function handleMouse(e: React.MouseEvent) {
    if (!cardRef.current) return
    const { left, top, width, height } = cardRef.current.getBoundingClientRect()
    const x = (e.clientX - left) / width
    const y = (e.clientY - top) / height

    setRotateX((y - 0.5) * -10)
    setRotateY((x - 0.5) * 10)
    setGlowPosition({ x: x * 100, y: y * 100 })
  }

  function handleLeave() {
    setRotateX(0)
    setRotateY(0)
    setGlowPosition({ x: 50, y: 50 })
  }

  return (
    <motion.div
      ref={cardRef}
      initial={{ opacity: 0, y: 40 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: '-50px' }}
      transition={{ duration: 0.8, delay, ease: [0.16, 1, 0.3, 1] }}
      onMouseMove={handleMouse}
      onMouseLeave={handleLeave}
      style={{
        transform: `perspective(1000px) rotateX(${rotateX}deg) rotateY(${rotateY}deg)`,
        transformStyle: 'preserve-3d',
      }}
      className={clsx(
        'relative rounded-2xl overflow-hidden transition-transform duration-200 ease-out',
        'bg-gradient-to-br from-graphite/80 to-obsidian/90',
        'border border-white/[0.06]',
        className
      )}
    >
      <div
        className="absolute inset-0 opacity-0 hover:opacity-100 transition-opacity duration-500 pointer-events-none"
        style={{
          background: `radial-gradient(600px circle at ${glowPosition.x}% ${glowPosition.y}%, rgba(0, 240, 255, 0.06), transparent 40%)`,
        }}
      />
      <div
        className="absolute inset-0 rounded-2xl pointer-events-none"
        style={{
          background: `radial-gradient(400px circle at ${glowPosition.x}% ${glowPosition.y}%, rgba(0, 240, 255, 0.15), transparent 40%)`,
          mask: 'linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0)',
          maskComposite: 'exclude',
          padding: '1px',
        }}
      />
      <div className="relative z-10 p-8">{children}</div>
    </motion.div>
  )
}
