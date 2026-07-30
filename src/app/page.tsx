import { LegacyHome } from '@/components/LegacyHome'
import { ReelBackdrop } from '@/components/reel/ReelBackdrop'

export default function Home() {
  return (
    <div className="relative isolate">
      <ReelBackdrop />
      <LegacyHome elevated />
    </div>
  )
}
