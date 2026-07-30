import type { ReelProject } from '@/data/projects'
import styles from './reel.module.css'

type ProjectMetadataProps = {
  project: ReelProject
}

export function ProjectMetadata({ project }: ProjectMetadataProps) {
  const entries = [
    ['Location', project.meta.location],
    ['Year', project.meta.year],
    ['Type', project.meta.typology],
    ...(project.meta.area ? [['Area', project.meta.area]] : []),
  ]

  return (
    <div className={styles.metadata} data-reel-meta>
      <p className={styles.description}>{project.description}</p>
      <dl className={styles.metadataList}>
        {entries.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <a
        className={styles.projectCta}
        href={project.media.src}
        target="_blank"
        rel="noreferrer"
      >
        View full image
        <span aria-hidden="true">↗</span>
      </a>
    </div>
  )
}
