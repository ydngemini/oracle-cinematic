import type { Metadata } from 'next'
import localFont from 'next/font/local'
import { SmoothScrollProvider } from '@/components/ui/SmoothScroll'
import './globals.css'

const oracleSans = localFont({
  src: './fonts/geist-latin.woff2',
  display: 'swap',
  variable: '--font-oracle-sans',
})

const oracleMono = localFont({
  src: './fonts/geist-mono-latin.woff2',
  display: 'swap',
  variable: '--font-oracle-mono',
})

export const metadata: Metadata = {
  title: 'ORACLE — Autonomous Intelligence Platform',
  description: 'Next-generation AI command center for autonomous real-estate intelligence.',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`dark ${oracleSans.variable} ${oracleMono.variable}`}>
      <body className="bg-black text-white antialiased overflow-x-hidden">
        <SmoothScrollProvider>
          {children}
        </SmoothScrollProvider>
      </body>
    </html>
  )
}
