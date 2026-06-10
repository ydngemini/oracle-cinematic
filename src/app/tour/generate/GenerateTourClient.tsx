'use client'

import { useRouter, useSearchParams } from 'next/navigation'
import { Suspense, useMemo, useState } from 'react'
import dynamic from 'next/dynamic'
import { MagneticButton } from '@/components/ui/MagneticButton'

const TourExperience = dynamic(
  () => import('@/components/tour/TourExperience').then(m => m.TourExperience),
  { ssr: false }
)

// ── Intake console — shown when no property params are present ──────────────
function UplinkConsole() {
  const router = useRouter()
  const [address, setAddress] = useState('')
  const [description, setDescription] = useState('')

  const initiate = () => {
    if (!address.trim()) return
    const params = new URLSearchParams({ address: address.trim() })
    if (description.trim()) params.set('description', description.trim())
    // Re-entering this route with params hands the input to the real
    // AI-generation pipeline (useTourData → backend) — no simulated steps.
    router.push(`/tour/generate?${params.toString()}`)
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-[#05070d] p-6 font-mono text-[#e0e8ff]">
      <div className="relative w-full max-w-2xl overflow-hidden border border-[rgba(0,240,255,0.3)] bg-[rgba(10,10,15,0.6)] p-8 backdrop-blur-md">
        {/* chrome */}
        <div className="absolute left-0 top-0 h-px w-full bg-gradient-to-r from-transparent via-[#00f0ff] to-transparent opacity-60" />

        <h1 className="mb-2 text-xl uppercase tracking-[0.3em] text-[rgba(0,240,255,0.85)]">
          Spatial Tour Generation
        </h1>
        <p className="mb-8 text-[11px] uppercase tracking-[0.2em] text-[rgba(224,232,255,0.35)]">
          Oracle AI Architect · property → digital twin
        </p>

        <div className="flex flex-col gap-4">
          <input
            type="text"
            placeholder="Target property address or APN…"
            className="w-full border border-[rgba(224,232,255,0.15)] bg-[#020408] p-4 text-sm text-[rgba(0,240,255,0.9)] placeholder-[rgba(224,232,255,0.25)] transition-colors focus:border-[rgba(0,240,255,0.6)] focus:outline-none"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && initiate()}
          />
          <textarea
            placeholder="Optional — describe the property (beds, finishes, views…)"
            rows={3}
            className="w-full resize-none border border-[rgba(224,232,255,0.15)] bg-[#020408] p-4 text-sm text-[rgba(224,232,255,0.8)] placeholder-[rgba(224,232,255,0.25)] transition-colors focus:border-[rgba(0,240,255,0.6)] focus:outline-none"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
          <MagneticButton
            onClick={initiate}
            className="mt-2 border border-[rgba(0,240,255,0.5)] bg-[rgba(0,240,255,0.08)] px-6 py-3 uppercase tracking-[0.3em] transition-all hover:bg-[rgba(0,240,255,0.2)]"
          >
            [ Initialize Uplink ]
          </MagneticButton>
        </div>
      </div>
    </div>
  )
}

function GenerateTourInner() {
  const params = useSearchParams()

  const propertyInput = useMemo(() => {
    const data: Record<string, unknown> = {}
    const address = params.get('address')
    const sqft = params.get('sqft')
    const bedrooms = params.get('bedrooms')
    const bathrooms = params.get('bathrooms')
    const price = params.get('price')
    const description = params.get('description')
    const features = params.get('features')

    if (address) data.address = address
    if (sqft) data.sqft = parseInt(sqft, 10)
    if (bedrooms) data.bedrooms = parseInt(bedrooms, 10)
    if (bathrooms) data.bathrooms = parseInt(bathrooms, 10)
    if (price) data.price = parseInt(price, 10)
    if (description) data.description = description
    if (features) data.features = features.split(',')

    return Object.keys(data).length > 0 ? data : undefined
  }, [params])

  if (!propertyInput) return <UplinkConsole />

  return <TourExperience propertyInput={propertyInput} />
}

export function GenerateTourClient() {
  return (
    <Suspense fallback={null}>
      <GenerateTourInner />
    </Suspense>
  )
}
