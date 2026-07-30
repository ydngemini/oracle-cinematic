export type ProjectMedia = {
  kind: 'image' | 'video'
  src: string
  poster?: string
  alt: string
  focalPoint?: `${number}% ${number}%`
  status: 'generated-placeholder' | 'approved'
}

export type ProjectMeta = {
  location: string
  year: string
  typology: string
  area?: string
}

export type ReelProject = {
  id: string
  title: string
  subtitle: string
  eyebrow: string
  description: string
  meta: ProjectMeta
  media: ProjectMedia
  accent: 'bronze' | 'amber' | 'concrete'
}

export const projects = [
  {
    id: 'fall-line-house',
    title: 'Fall Line House',
    subtitle: 'A quiet line held against the mountain.',
    eyebrow: 'Residence / 01',
    description:
      'A concrete frame follows the natural break in the land, opening a measured interior to weather, stone, and the distant ridge.',
    meta: {
      location: 'Blue Ridge, Virginia',
      year: '2026',
      typology: 'Mountain residence',
      area: '4,800 sq ft',
    },
    media: {
      kind: 'image',
      src: '/projects/fall-line-house/hero.webp',
      alt: 'Concrete house embedded in a dark forested ridgeline at blue hour.',
      focalPoint: '60% 50%',
      status: 'generated-placeholder',
    },
    accent: 'concrete',
  },
  {
    id: 'threshold-water-side',
    title: 'Threshold / Water Side',
    subtitle: 'The room ends where the horizon begins.',
    eyebrow: 'Pavilion / 02',
    description:
      'A monolithic threshold compresses the interior before releasing it toward still water and the first light above the opposite shore.',
    meta: {
      location: 'Deep Creek, Maryland',
      year: '2025',
      typology: 'Waterside pavilion',
      area: '1,920 sq ft',
    },
    media: {
      kind: 'image',
      src: '/projects/threshold-water-side/hero.webp',
      alt: 'Concrete lakeside pavilion framing still water and a misty dawn horizon.',
      focalPoint: '54% 48%',
      status: 'generated-placeholder',
    },
    accent: 'amber',
  },
  {
    id: 'timber-core',
    title: 'Timber Core',
    subtitle: 'Warm structure inside a field of shadow.',
    eyebrow: 'Interior / 03',
    description:
      'A finely joined oak volume gathers circulation, light, and storage into one inhabitable core set against raw concrete planes.',
    meta: {
      location: 'Hudson Valley, New York',
      year: '2024',
      typology: 'Residential interior',
      area: '3,260 sq ft',
    },
    media: {
      kind: 'image',
      src: '/projects/timber-core/hero.webp',
      alt: 'Warm vertical timber core and stair within a dark concrete interior.',
      focalPoint: '50% 48%',
      status: 'generated-placeholder',
    },
    accent: 'bronze',
  },
] as const satisfies readonly ReelProject[]
