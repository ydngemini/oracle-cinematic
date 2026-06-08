'use client'

import dynamic from 'next/dynamic'

const TourExperience = dynamic(
  () => import('./TourExperience').then(m => m.TourExperience),
  { ssr: false }
)

export function TourLoader() {
  return <TourExperience />
}
