import type { CSSProperties } from 'react'
import type { ReelProject } from '@/data/projects'
import styles from './reel.module.css'

type ProjectProgressProps = {
  activeIndex: number
  projects: readonly ReelProject[]
  onSelect: (index: number) => void
}

export function ProjectProgress({
  activeIndex,
  projects,
  onSelect,
}: ProjectProgressProps) {
  const progress =
    projects.length > 1 ? activeIndex / (projects.length - 1) : 1

  return (
    <nav className={styles.progress} aria-label="Select a project">
      <div className={styles.progressTrack} aria-hidden="true">
        <span
          style={{ '--reel-progress': progress } as CSSProperties}
          className={styles.progressFill}
        />
      </div>
      <ol>
        {projects.map((project, index) => (
          <li key={project.id}>
            <button
              type="button"
              aria-current={activeIndex === index ? 'true' : undefined}
              aria-label={`Go to project ${index + 1}: ${project.title}`}
              onClick={() => onSelect(index)}
            >
              {String(index + 1).padStart(2, '0')}
            </button>
          </li>
        ))}
      </ol>
    </nav>
  )
}
