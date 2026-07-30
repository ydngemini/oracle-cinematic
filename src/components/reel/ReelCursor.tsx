'use client'

import { useEffect, useRef } from 'react'
import styles from './reel.module.css'

export function ReelCursor() {
  const cursorRef = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    const cursor = cursorRef.current
    const enabled = window.matchMedia(
      '(pointer: fine) and (prefers-reduced-motion: no-preference)',
    )

    if (!cursor || !enabled.matches) return

    const moveCursor = (event: PointerEvent) => {
      cursor.style.transform = `translate3d(${event.clientX}px, ${event.clientY}px, 0) translate(-50%, -50%)`
      cursor.dataset.visible = 'true'
    }
    const hideCursor = () => {
      cursor.dataset.visible = 'false'
    }
    const updateHoverState = (event: PointerEvent) => {
      const target = event.target
      cursor.dataset.interactive =
        target instanceof Element && Boolean(target.closest('a, button'))
          ? 'true'
          : 'false'
    }

    window.addEventListener('pointermove', moveCursor, { passive: true })
    document.addEventListener('pointerover', updateHoverState, { passive: true })
    document.addEventListener('pointerleave', hideCursor)

    return () => {
      window.removeEventListener('pointermove', moveCursor)
      document.removeEventListener('pointerover', updateHoverState)
      document.removeEventListener('pointerleave', hideCursor)
    }
  }, [])

  return <span ref={cursorRef} className={styles.reelCursor} aria-hidden="true" />
}
