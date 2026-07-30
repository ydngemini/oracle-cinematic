import type { ReelProject } from '@/data/projects'
import styles from './reel.module.css'

type ProjectTitleRailProps = {
  project: ReelProject
  index: number
}

export function ProjectTitleRail({
  project,
  index,
}: ProjectTitleRailProps) {
  return (
    <header className={styles.titleRail} data-reel-title>
      <p className={styles.eyebrow}>
        <span>{String(index + 1).padStart(2, '0')}</span>
        {project.eyebrow}
      </p>
      <h2 id={`${project.id}-title`} className={styles.projectTitle}>
        {project.title}
      </h2>
      <p className={styles.subtitle}>{project.subtitle}</p>
    </header>
  )
}
