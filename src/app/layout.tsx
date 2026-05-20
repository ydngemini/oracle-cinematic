import type { Metadata } from 'next'
import { SmoothScrollProvider } from '@/components/ui/SmoothScroll'
import './globals.css'

export const metadata: Metadata = {
  title: 'ORACLE — Autonomous Intelligence Platform',
  description: 'Next-generation AI command center for autonomous real-estate intelligence.',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="bg-black text-white antialiased overflow-x-hidden">
        <SmoothScrollProvider>
          {children}
        </SmoothScrollProvider>
      </body>
    </html>
  )
}
