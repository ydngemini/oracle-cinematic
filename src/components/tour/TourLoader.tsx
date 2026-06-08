'use client'

import dynamic from 'next/dynamic'
import { TourErrorBoundary } from './TourErrorBoundary'

const TourExperience = dynamic(
  () => import('./TourExperience').then(m => m.TourExperience),
  { ssr: false }
)

export function TourLoader() {
  return (
    <TourErrorBoundary>
      <TourExperience />
    </TourErrorBoundary>
  )
}
