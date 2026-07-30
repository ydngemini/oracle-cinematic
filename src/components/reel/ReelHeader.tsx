import Link from 'next/link'
import styles from './reel.module.css'

type ReelHeaderProps = {
  activeIndex: number
  projectCount: number
  onSelectProject: (index: number) => void
}

function formatIndex(index: number) {
  return String(index + 1).padStart(2, '0')
}

export function ReelHeader({
  activeIndex,
  projectCount,
  onSelectProject,
}: ReelHeaderProps) {
  return (
    <header className={styles.reelHeader}>
      <Link className={styles.brand} href="/" aria-label="NEOH home">
        <span>NEOH</span>
        <span>Spatial studies</span>
      </Link>

      <nav className={styles.desktopNav} aria-label="Reel navigation">
        <button type="button" onClick={() => onSelectProject(0)}>
          Projects
        </button>
        <Link href="/tour">Tour</Link>
        <Link href="/legacy">Legacy</Link>
      </nav>

      <p className={styles.headerCounter} aria-live="polite" aria-atomic="true">
        <span>{formatIndex(activeIndex)}</span>
        <span aria-hidden="true"> / </span>
        <span>{formatIndex(projectCount - 1)}</span>
        <span className={styles.visuallyHidden}> project selected</span>
      </p>
    </header>
  )
}
