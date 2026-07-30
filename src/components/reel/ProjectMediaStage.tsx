'use client'

import Image from 'next/image'
import { useEffect, useRef } from 'react'
import type { ProjectMedia, ReelProject } from '@/data/projects'
import styles from './reel.module.css'

type ProjectMediaStageProps = {
  project: ReelProject
  priority?: boolean
}

function ProjectVideo({ media }: { media: ProjectMedia }) {
  const videoRef = useRef<HTMLVideoElement>(null)

  useEffect(() => {
    const video = videoRef.current
    if (!video) return

    const reducedMotion = window.matchMedia(
      '(prefers-reduced-motion: reduce)',
    ).matches
    const connection = (
      navigator as Navigator & { connection?: { saveData?: boolean } }
    ).connection

    if (reducedMotion || connection?.saveData) return

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          void video.play().catch(() => undefined)
        } else {
          video.pause()
        }
      },
      { threshold: 0.55 },
    )

    observer.observe(video)
    return () => {
      observer.disconnect()
      video.pause()
    }
  }, [])

  return (
    <video
      ref={videoRef}
      className={styles.mediaAsset}
      src={media.src}
      poster={media.poster}
      aria-label={media.alt}
      muted
      playsInline
      loop
      preload="metadata"
      data-reel-asset
    />
  )
}

export function ProjectMediaStage({
  project,
  priority = false,
}: ProjectMediaStageProps) {
  const { media } = project

  return (
    <figure
      className={`${styles.mediaStage} ${styles[`accent${project.accent}`]}`}
      data-reel-media
    >
      <div className={styles.mediaInner}>
        {media.kind === 'image' ? (
          <Image
            className={styles.mediaAsset}
            src={media.src}
            alt={media.alt}
            fill
            priority={priority}
            sizes="(max-width: 899px) 100vw, 62vw"
            quality={88}
            style={{ objectPosition: media.focalPoint ?? '50% 50%' }}
            data-reel-asset
          />
        ) : (
          <ProjectVideo media={media} />
        )}
        <span className={styles.mediaShade} aria-hidden="true" />
      </div>
      {media.status === 'generated-placeholder' ? (
        <figcaption className={styles.assetStatus}>Concept image</figcaption>
      ) : null}
    </figure>
  )
}
