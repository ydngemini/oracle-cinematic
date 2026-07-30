'use client'

import Link from 'next/link'
import { useEffect, useId, useRef, useState } from 'react'
import type { ReelProject } from '@/data/projects'
import styles from './reel.module.css'

type ReelMobileNavigationProps = {
  projects: readonly ReelProject[]
  onSelect: (index: number) => void
}

export function ReelMobileNavigation({
  projects,
  onSelect,
}: ReelMobileNavigationProps) {
  const [isOpen, setIsOpen] = useState(false)
  const menuId = useId()
  const triggerRef = useRef<HTMLButtonElement>(null)
  const firstItemRef = useRef<HTMLButtonElement>(null)

  const closeMenu = () => {
    setIsOpen(false)
    requestAnimationFrame(() => triggerRef.current?.focus())
  }

  useEffect(() => {
    if (!isOpen) return

    firstItemRef.current?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeMenu()
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isOpen])

  return (
    <div className={styles.mobileNavigation}>
      <button
        ref={triggerRef}
        className={styles.menuTrigger}
        type="button"
        aria-expanded={isOpen}
        aria-controls={menuId}
        onClick={() => (isOpen ? closeMenu() : setIsOpen(true))}
      >
        <span>Index</span>
        <span aria-hidden="true">{isOpen ? 'Close' : 'Open'}</span>
      </button>

      <nav
        id={menuId}
        className={styles.mobileMenu}
        aria-label="Project index"
        hidden={!isOpen}
      >
        <p>Selected works</p>
        <ol>
          {projects.map((project, index) => (
            <li key={project.id}>
              <button
                ref={index === 0 ? firstItemRef : undefined}
                type="button"
                onClick={() => {
                  onSelect(index)
                  closeMenu()
                }}
              >
                <span>{String(index + 1).padStart(2, '0')}</span>
                <span>{project.title}</span>
              </button>
            </li>
          ))}
        </ol>
        <Link href="/">NEOH platform</Link>
      </nav>
    </div>
  )
}
