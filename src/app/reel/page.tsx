import type { Metadata } from 'next'
import { ReelExperience } from '@/components/reel/ReelExperience'

export const metadata: Metadata = {
  title: 'Architectural Studies — NEOH',
  description:
    'A cinematic sequence of spatial studies in concrete, timber, water, and light.',
}

export default function ReelPage() {
  return <ReelExperience />
}
