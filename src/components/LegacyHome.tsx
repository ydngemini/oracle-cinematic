import { CapabilitiesSection } from '@/components/sections/CapabilitiesSection'
import { FeatureGrid } from '@/components/sections/FeatureGrid'
import { FinalCTA } from '@/components/sections/FinalCTA'
import { HeroSection } from '@/components/sections/HeroSection'
import { MetricsSection } from '@/components/sections/MetricsSection'
import { NeuralVizSection } from '@/components/sections/NeuralVizSection'
import { TerminalSection } from '@/components/sections/TerminalSection'
import { TimelineSection } from '@/components/sections/TimelineSection'

type LegacyHomeProps = {
  elevated?: boolean
}

export function LegacyHome({ elevated = false }: LegacyHomeProps) {
  return (
    <main className={`relative noise-overlay ${elevated ? 'z-10' : ''}`}>
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
