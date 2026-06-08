'use client'

import { useSearchParams } from 'next/navigation'
import { Suspense, useMemo } from 'react'
import dynamic from 'next/dynamic'

const TourExperience = dynamic(
  () => import('@/components/tour/TourExperience').then(m => m.TourExperience),
  { ssr: false }
)

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

  return <TourExperience propertyInput={propertyInput} />
}

export function GenerateTourClient() {
  return (
    <Suspense fallback={null}>
      <GenerateTourInner />
    </Suspense>
  )
}
