import type { Metadata } from 'next'
import { TourLoader } from '@/components/tour/TourLoader'

export const metadata: Metadata = {
  title: 'Oracle — Spatial Tour',
  description: 'Immersive 3D virtual tour powered by Oracle',
}

export default function TourPage() {
  return <TourLoader />
}
