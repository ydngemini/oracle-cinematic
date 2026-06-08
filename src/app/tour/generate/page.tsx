import type { Metadata } from 'next'
import { GenerateTourClient } from './GenerateTourClient'

export const metadata: Metadata = {
  title: 'Oracle — AI Tour Generator',
  description: 'Generate an immersive 3D tour from property data',
}

export default function GenerateTourPage() {
  return <GenerateTourClient />
}
