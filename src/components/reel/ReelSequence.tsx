'use client'

import Link from 'next/link'
import { useRef } from 'react'
import type { ReelProject } from '@/data/projects'
import { ProjectMediaStage } from './ProjectMediaStage'
import { ProjectMetadata } from './ProjectMetadata'
import { ProjectProgress } from './ProjectProgress'
import { ProjectTitleRail } from './ProjectTitleRail'
import { ReelCursor } from './ReelCursor'
import { ReelHeader } from './ReelHeader'
import { ReelMobileNavigation } from './ReelMobileNavigation'
import { useReelSequence } from './useReelSequence'
import styles from './reel.module.css'

type ReelSequenceProps = {
  projects: readonly ReelProject[]
}

export function ReelSequence({ projects }: ReelSequenceProps) {
  const sequenceRef = useRef<HTMLElement>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const { activeIndex, goToProject } = useReelSequence(
    sequenceRef,
    stageRef,
    projects.length,
  )

  return (
    <>
      <ReelHeader
        activeIndex={activeIndex}
        projectCount={projects.length}
        onSelectProject={goToProject}
      />

      <main id="reel-main" className={styles.reelMain}>
        <section
          ref={sequenceRef}
          className={styles.sequence}
          aria-labelledby="reel-title"
        >
          <h1 id="reel-title" className={styles.visuallyHidden}>
            NEOH architectural studies
          </h1>

          <div ref={stageRef} className={styles.stage}>
            <div className={styles.projectStack}>
              {projects.map((project, index) => (
                <article
                  id={`project-${index}`}
                  className={styles.project}
                  data-active={index === activeIndex}
                  data-reel-project
                  aria-labelledby={`${project.id}-title`}
                  key={project.id}
                >
                  <ProjectTitleRail project={project} index={index} />
                  <ProjectMediaStage project={project} priority={index === 0} />
                  <ProjectMetadata project={project} />
                </article>
              ))}
            </div>

            <ProjectProgress
              activeIndex={activeIndex}
              projects={projects}
              onSelect={goToProject}
            />
          </div>
        </section>

        <footer id="reel-index" className={styles.reelFooter}>
          <p>NEOH / Spatial studies</p>
          <p>Concrete · Timber · Water · Light</p>
          <Link href="/">Return to the intelligence platform</Link>
        </footer>
      </main>

      <ReelMobileNavigation projects={projects} onSelect={goToProject} />
      <ReelCursor />
    </>
  )
}
