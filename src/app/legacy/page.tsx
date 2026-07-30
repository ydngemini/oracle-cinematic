import type { Metadata } from 'next'
import { LegacyHome } from '@/components/LegacyHome'

export const metadata: Metadata = {
  title: 'ORACLE — Legacy Experience',
  description: 'The original ORACLE autonomous intelligence experience.',
}

export default function LegacyPage() {
  return <LegacyHome />
}
