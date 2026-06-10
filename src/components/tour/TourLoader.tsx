'use client'

import dynamic from 'next/dynamic'
import { useCallback, useEffect, useState } from 'react'
import { AnimatePresence } from 'framer-motion'
import { TourErrorBoundary } from './TourErrorBoundary'
import { TourEntry } from './TourEntry'

const TourExperience = dynamic(
  () => import('./TourExperience').then(m => m.TourExperience),
  { ssr: false }
)

export function TourLoader() {
  const [entered, setEntered] = useState(false)
  const [warm, setWarm] = useState(false)

  // Pre-mount the 3D scene behind the opaque entry once its reveal settles,
  // so the aperture wipe lands on an already-initialized canvas.
  useEffect(() => {
    const id = setTimeout(() => setWarm(true), 1800)
    return () => clearTimeout(id)
  }, [])

  const onEnter = useCallback(() => {
    setWarm(true)
    setEntered(true)
  }, [])

  return (
    <TourErrorBoundary>
      {(warm || entered) && <TourExperience />}
      <AnimatePresence>{!entered && <TourEntry onEnter={onEnter} />}</AnimatePresence>
    </TourErrorBoundary>
  )
}
