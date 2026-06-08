'use client'

import { Component, type ReactNode } from 'react'

interface Props { children: ReactNode }
interface State { error: Error | null }

export class TourErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="fixed inset-0 z-50 grid place-items-center bg-[#05070d]">
          <div className="max-w-md text-center px-6">
            <div className="mb-4 text-[11px] font-semibold uppercase tracking-[0.3em] text-[rgba(239,68,68,0.8)]">
              Rendering Error
            </div>
            <p className="text-[14px] text-[rgba(224,232,255,0.7)] leading-relaxed mb-6">
              The 3D tour failed to render. This usually means your browser doesn&apos;t support WebGL2
              or ran out of GPU memory.
            </p>
            <button
              type="button"
              onClick={() => this.setState({ error: null })}
              className="rounded-xl border border-[rgba(0,240,255,0.35)] bg-[rgba(0,240,255,0.08)] px-5 py-2.5 text-[13px] font-semibold text-[#7df0ff] transition hover:bg-[rgba(0,240,255,0.14)]"
            >
              Retry
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
