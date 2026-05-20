'use client'

import { HeroSection } from '@/components/sections/HeroSection'
import { CapabilitiesSection } from '@/components/sections/CapabilitiesSection'
import { NeuralVizSection } from '@/components/sections/NeuralVizSection'
import { TerminalSection } from '@/components/sections/TerminalSection'
import { MetricsSection } from '@/components/sections/MetricsSection'
import { FeatureGrid } from '@/components/sections/FeatureGrid'
import { TimelineSection } from '@/components/sections/TimelineSection'
import { FinalCTA } from '@/components/sections/FinalCTA'

export default function Home() {
  return (
    <main className="relative noise-overlay">
      <HeroSection />
      <CapabilitiesSection />
      <NeuralVizSection />
      <TerminalSection />
      <MetricsSection />
      <FeatureGrid />
      <TimelineSection />
      <FinalCTA />
    </main>
  )
}
