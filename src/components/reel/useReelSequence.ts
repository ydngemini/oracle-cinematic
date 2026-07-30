'use client'

import {
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react'
import { useLenis } from '@/components/ui/SmoothScroll'

type ReelScrollTrigger = {
  start: number
  end: number
}

type UseReelSequenceResult = {
  activeIndex: number
  goToProject: (index: number) => void
}

export function useReelSequence(
  rootRef: RefObject<HTMLElement | null>,
  stageRef: RefObject<HTMLDivElement | null>,
  projectCount: number,
): UseReelSequenceResult {
  const lenis = useLenis()
  const triggerRef = useRef<ReelScrollTrigger | null>(null)
  const pendingIndexRef = useRef<number | null>(null)
  const activeIndexRef = useRef(0)
  const [activeIndex, setActiveIndex] = useState(0)

  const updateActiveIndex = useCallback((nextIndex: number) => {
    if (activeIndexRef.current === nextIndex) return
    activeIndexRef.current = nextIndex
    setActiveIndex(nextIndex)
  }, [])

  const goToProject = useCallback(
    (requestedIndex: number) => {
      const index = Math.min(Math.max(requestedIndex, 0), projectCount - 1)
      const trigger = triggerRef.current
      const hasCinematicLayout = window.matchMedia(
        '(min-width: 900px) and (prefers-reduced-motion: no-preference)',
      ).matches

      if (hasCinematicLayout && projectCount > 1) {
        if (!trigger) {
          pendingIndexRef.current = index
          return
        }

        const progress = index / (projectCount - 1)
        const destination =
          trigger.start + (trigger.end - trigger.start) * progress
        if (lenis) {
          lenis.scrollTo(destination, { duration: 1.35 })
        } else {
          window.scrollTo({ top: destination, behavior: 'smooth' })
        }
        return
      }

      const project = document.getElementById(`project-${index}`)
      if (!project) return

      if (lenis) {
        lenis.scrollTo(project, { duration: 1, offset: -24 })
      } else {
        project.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }
    },
    [lenis, projectCount],
  )

  useEffect(() => {
    const root = rootRef.current
    const stage = stageRef.current
    if (!root || !stage || !lenis) return

    let disposed = false
    let refreshTimer: ReturnType<typeof setTimeout> | undefined
    let removeGsap: (() => void) | undefined

    const setup = async () => {
      const [{ gsap }, { ScrollTrigger }] = await Promise.all([
        import('gsap'),
        import('gsap/ScrollTrigger'),
      ])

      if (disposed) return

      gsap.registerPlugin(ScrollTrigger)

      const syncScrollTrigger = () => ScrollTrigger.update()
      lenis.on('scroll', syncScrollTrigger)

      const scheduleRefresh = () => {
        if (refreshTimer) clearTimeout(refreshTimer)
        refreshTimer = setTimeout(() => ScrollTrigger.refresh(), 120)
      }

      const media = Array.from(root.querySelectorAll('img, video'))
      media.forEach((element) => {
        element.addEventListener('load', scheduleRefresh, { once: true })
        element.addEventListener('loadedmetadata', scheduleRefresh, { once: true })
      })
      window.addEventListener('resize', scheduleRefresh, { passive: true })
      document.fonts?.ready.then(scheduleRefresh).catch(() => undefined)

      const matchMedia = gsap.matchMedia()

      matchMedia.add(
        '(min-width: 900px) and (prefers-reduced-motion: no-preference)',
        () => {
          const projectPanels = gsap.utils.toArray<HTMLElement>(
            '[data-reel-project]',
            root,
          )

          if (projectPanels.length < 2) return

          gsap.set(projectPanels, { autoAlpha: 0 })
          gsap.set(projectPanels[0], { autoAlpha: 1, zIndex: 2 })

          const timeline = gsap.timeline({
            defaults: { ease: 'power3.inOut' },
            scrollTrigger: {
              trigger: root,
              pin: stage,
              start: 'top top',
              end: () =>
                `+=${Math.max(2400, window.innerHeight * projectPanels.length)}`,
              scrub: 1,
              anticipatePin: 1,
              invalidateOnRefresh: true,
              onUpdate: (self) => {
                const nextIndex = Math.round(
                  self.progress * (projectPanels.length - 1),
                )
                updateActiveIndex(nextIndex)
              },
            },
          })

          projectPanels.slice(1).forEach((panel, index) => {
            const previous = projectPanels[index]
            const position = index + 1
            const previousTitle = previous.querySelector('[data-reel-title]')
            const previousMeta = previous.querySelector('[data-reel-meta]')
            const previousMedia = previous.querySelector('[data-reel-media]')
            const title = panel.querySelector('[data-reel-title]')
            const meta = panel.querySelector('[data-reel-meta]')
            const mediaFrame = panel.querySelector('[data-reel-media]')
            const mediaAsset = panel.querySelector('[data-reel-asset]')

            timeline
              .set(panel, { autoAlpha: 1, zIndex: position + 2 }, position - 0.1)
              .to(
                previousTitle,
                { autoAlpha: 0, yPercent: -18, duration: 0.42 },
                position - 0.32,
              )
              .to(
                previousMeta,
                { autoAlpha: 0, y: -16, duration: 0.36 },
                position - 0.25,
              )
              .to(
                previousMedia,
                { autoAlpha: 0, scale: 0.985, duration: 0.66 },
                position - 0.18,
              )
              .fromTo(
                mediaFrame,
                { clipPath: 'inset(0 100% 0 0)' },
                {
                  clipPath: 'inset(0 0% 0 0)',
                  duration: 0.78,
                  ease: 'power3.inOut',
                },
                position - 0.08,
              )
              .fromTo(
                mediaAsset,
                { scale: 1.04 },
                { scale: 1, duration: 0.9, ease: 'power2.out' },
                position - 0.08,
              )
              .fromTo(
                title,
                { autoAlpha: 0, yPercent: 18 },
                { autoAlpha: 1, yPercent: 0, duration: 0.58 },
                position + 0.08,
              )
              .fromTo(
                meta,
                { autoAlpha: 0, y: 18 },
                { autoAlpha: 1, y: 0, duration: 0.5 },
                position + 0.16,
              )
              .set(previous, { autoAlpha: 0 }, position + 0.48)
          })

          triggerRef.current = timeline.scrollTrigger ?? null

          if (pendingIndexRef.current !== null && timeline.scrollTrigger) {
            const requestedIndex = pendingIndexRef.current
            pendingIndexRef.current = null
            const progress = requestedIndex / (projectPanels.length - 1)
            const destination =
              timeline.scrollTrigger.start +
              (timeline.scrollTrigger.end - timeline.scrollTrigger.start) *
                progress
            lenis.scrollTo(destination, { duration: 1.35 })
          }

          scheduleRefresh()

          return () => {
            triggerRef.current = null
            timeline.scrollTrigger?.kill()
            timeline.kill()
            gsap.set(projectPanels, { clearProps: 'all' })
          }
        },
      )

      removeGsap = () => {
        matchMedia.revert()
        lenis.off('scroll', syncScrollTrigger)
        media.forEach((element) => {
          element.removeEventListener('load', scheduleRefresh)
          element.removeEventListener('loadedmetadata', scheduleRefresh)
        })
        window.removeEventListener('resize', scheduleRefresh)
        if (refreshTimer) clearTimeout(refreshTimer)
      }
    }

    void setup()

    return () => {
      disposed = true
      removeGsap?.()
    }
  }, [lenis, projectCount, rootRef, stageRef, updateActiveIndex])

  return { activeIndex, goToProject }
}
