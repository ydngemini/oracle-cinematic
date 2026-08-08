import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { hasHighMotionBudget } from './motion/AdaptiveViewTransition';
import styles from './ReelExperience.module.css';

const projects = [
  {
    id: 'fall-line-house', index: '01', eyebrow: 'Residential / Ridge Line', title: 'Fall Line\nHouse',
    subtitle: 'A concrete residence held between the contour of the land and the evening horizon.',
    location: 'High Desert, Utah', year: '2026', typology: 'Private residence', area: '3,840 sq ft',
    src: '/projects/fall-line-house/hero.webp', alt: 'Concrete residence on a ridgeline at blue hour with warm interior lighting',
  },
  {
    id: 'threshold-water-side', index: '02', eyebrow: 'Hospitality / Water Edge', title: 'Threshold /\nWater Side',
    subtitle: 'A quiet pavilion where shadow, reflection, and the shoreline set the pace of arrival.',
    location: 'Great Lakes, Michigan', year: '2026', typology: 'Retreat pavilion', area: '2,190 sq ft',
    src: '/projects/threshold-water-side/hero.webp', alt: 'Modern lakeside pavilion at dawn reflected in dark water',
  },
  {
    id: 'timber-core', index: '03', eyebrow: 'Interior / Material Study', title: 'Timber\nCore',
    subtitle: 'An interior organized around a single oak volume, with concrete planes and low amber light.',
    location: 'Chicago, Illinois', year: '2026', typology: 'Adaptive interior', area: '5,120 sq ft',
    src: '/projects/timber-core/hero.webp', alt: 'Dark architectural interior featuring a monumental oak core and concrete planes',
  },
];

function EstateBackground({ className }) {
  const videoRef = useRef(null);
  const reducedMotion = useReducedMotion();
  const [saveData, setSaveData] = useState(
    () => Boolean(navigator.connection?.saveData),
  );
  const showMotion = !reducedMotion && !saveData;

  useEffect(() => {
    const connection = navigator.connection;
    if (!connection?.addEventListener) return undefined;
    const updateSaveData = () => setSaveData(Boolean(connection.saveData));
    connection.addEventListener('change', updateSaveData);
    return () => connection.removeEventListener('change', updateSaveData);
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !showMotion) return undefined;

    const syncPlayback = () => {
      if (document.hidden) {
        video.pause();
        return;
      }
      void video.play().catch(() => {
        // The poster remains visible if a browser blocks ambient autoplay.
      });
    };

    document.addEventListener('visibilitychange', syncPlayback);
    video.addEventListener('canplay', syncPlayback);
    syncPlayback();

    return () => {
      document.removeEventListener('visibilitychange', syncPlayback);
      video.removeEventListener('canplay', syncPlayback);
      video.pause();
    };
  }, [showMotion]);

  if (showMotion) {
    return (
      <video
        ref={videoRef}
        className={className}
        poster="/media/mountain-waterfall-estate-v1.webp"
        autoPlay
        muted
        loop
        playsInline
        preload="metadata"
        aria-hidden="true"
        tabIndex={-1}
        disablePictureInPicture
      >
        <source
          src="/media/mountain-waterfall-estate-v1-mobile.mp4"
          type="video/mp4"
          media="(max-width: 759px)"
        />
        <source src="/media/mountain-waterfall-estate-v1.mp4" type="video/mp4" />
      </video>
    );
  }

  return (
    <img
      className={className}
      src="/media/mountain-waterfall-estate-v1.webp"
      alt=""
      aria-hidden="true"
      decoding="async"
      fetchPriority="high"
    />
  );
}

export function ReelBackdrop() {
  return (
    <div className={styles.backdrop} aria-hidden="true">
      <EstateBackground className={styles.backdropMedia} />
    </div>
  );
}

function ProjectBody({ project, index, eagerMedia }) {
  return (
    <article className={styles.project} id={project.id} data-project-index={index} data-reel-project>
      <div className={styles.projectRail}>
        <p className={styles.eyebrow}><span>{project.index}</span>{project.eyebrow}</p>
        <h1>{project.title.split('\n').map((line) => <span key={line}>{line}</span>)}</h1>
        <p className={styles.description}>{project.subtitle}</p>
      </div>
      <figure className={styles.mediaStage}>
        <img
          src={project.src}
          alt={project.alt}
          loading={index === 0 || eagerMedia ? 'eager' : 'lazy'}
          decoding="async"
          fetchPriority={index === 0 ? 'high' : 'auto'}
        />
        <figcaption>Selected work / Neoh editorial studies</figcaption>
      </figure>
      <dl className={styles.metadata}>
        <div><dt>Location</dt><dd>{project.location}</dd></div>
        <div><dt>Completion</dt><dd>{project.year}</dd></div>
        <div><dt>Program</dt><dd>{project.typology}</dd></div>
        <div><dt>Area</dt><dd>{project.area}</dd></div>
      </dl>
    </article>
  );
}

export function ReelExperience() {
  const scrollRef = useRef(null);
  const sequenceRef = useRef(null);
  const pinRef = useRef(null);
  const menuTriggerRef = useRef(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false);
  const [wideViewport, setWideViewport] = useState(
    () => window.matchMedia('(min-width: 760px)').matches,
  );
  const reducedMotion = useReducedMotion();
  const pinnedSequence = !reducedMotion && wideViewport && hasHighMotionBudget();

  useEffect(() => {
    const mediaQuery = window.matchMedia('(min-width: 760px)');
    const updateViewport = () => setWideViewport(mediaQuery.matches);
    mediaQuery.addEventListener('change', updateViewport);
    return () => mediaQuery.removeEventListener('change', updateViewport);
  }, []);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const close = (event) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setMenuOpen(false);
      window.requestAnimationFrame(() => menuTriggerRef.current?.focus({ preventScroll: true }));
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [menuOpen]);

  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    const sequence = sequenceRef.current;
    const pin = pinRef.current;
    if (!scroller || !sequence || !pin || !pinnedSequence) return undefined;

    let cancelled = false;
    let ctx;

    void Promise.all([import('gsap'), import('gsap/ScrollTrigger')]).then(
      ([gsapModule, scrollTriggerModule]) => {
        if (cancelled) return;
        const gsap = gsapModule.gsap;
        const ScrollTrigger = scrollTriggerModule.ScrollTrigger;
        gsap.registerPlugin(ScrollTrigger);

        ctx = gsap.context(() => {
          const layers = gsap.utils.toArray(`.${styles.project}`);
          const step = 1;
          const mediaLayers = layers.map((layer) => layer.querySelector(`.${styles.mediaStage}`));
          gsap.set(layers, { autoAlpha: 0, y: 30, force3D: true });
          gsap.set(layers[0], { autoAlpha: 1, y: 0 });
          gsap.set(mediaLayers, { scale: 1, autoAlpha: 1, force3D: true });

          const timeline = gsap.timeline();
          layers.slice(1).forEach((layer, index) => {
            const previous = layers[index];
            const media = layer.querySelector(`.${styles.mediaStage}`);
            const at = (index + 1) * step;
            timeline
              .to(previous, { autoAlpha: 0, y: -28, duration: 0.42, ease: 'power2.out', force3D: true }, at)
              .set(layer, { autoAlpha: 1, y: 22 }, at)
              .fromTo(
                media,
                { autoAlpha: 0.18, scale: 1.045 },
                { autoAlpha: 1, scale: 1, duration: 0.78, ease: 'power3.inOut', force3D: true },
                at,
              )
              .to(layer, { y: 0, duration: 0.64, ease: 'power2.out', force3D: true }, at + 0.1);
          });

          ScrollTrigger.create({
            trigger: sequence,
            scroller,
            start: 'top top',
            end: () => `+=${Math.round(window.innerHeight * projects.length * 1.05)}`,
            pin,
            pinSpacing: true,
            scrub: 0.85,
            animation: timeline,
            invalidateOnRefresh: true,
            onUpdate: (self) => setActiveIndex(
              Math.min(projects.length - 1, Math.floor(self.progress * projects.length)),
            ),
          });
        }, sequence);
        ScrollTrigger.refresh();
      },
    );

    return () => {
      cancelled = true;
      ctx?.revert();
    };
  }, [pinnedSequence]);

  const goToProject = (index) => {
    const scroller = scrollRef.current;
    const sequence = sequenceRef.current;
    if (!scroller || !sequence) return;
    setActiveIndex(index);
    const target = pinnedSequence
      ? sequence.offsetTop + (window.innerHeight * 1.05 * index)
      : document.getElementById(projects[index].id)?.offsetTop ?? 0;
    scroller.scrollTo({ top: target, behavior: reducedMotion ? 'auto' : 'smooth' });
    setMenuOpen(false);
    window.setTimeout(() => menuTriggerRef.current?.focus({ preventScroll: true }), 0);
  };

  const activeProject = projects[activeIndex];
  return (
    <main
      className={styles.reel}
      ref={scrollRef}
      id="reel-main"
      data-reel-mode={pinnedSequence ? 'pinned' : 'static'}
    >
      <a className={styles.skipLink} href="#fall-line-house">Skip to project sequence</a>
      <div className={styles.reelAtmosphere} aria-hidden="true"><EstateBackground className={styles.reelMedia} /></div>
      <motion.header className={styles.header} initial={{ opacity: 0, y: -12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.8, ease: [0.16, 1, 0.3, 1] }}>
        <a className={styles.brand} href="/" aria-label="Return to Neoh"><span>Neoh</span><small>Field Office / 01</small></a>
        <nav className={styles.desktopNav} aria-label="Project navigation">
          {projects.map((project, index) => <button className={activeIndex === index ? styles.activeNav : undefined} key={project.id} type="button" onClick={() => goToProject(index)}>{project.index} / {project.title.replace('\n', ' ')}</button>)}
        </nav>
        <button type="button" ref={menuTriggerRef} className={styles.menuButton} aria-expanded={menuOpen} aria-controls="reel-mobile-nav" onClick={() => setMenuOpen((open) => !open)}>{menuOpen ? 'Close' : 'Index'}</button>
        <span className={styles.counter} aria-live="polite">{activeProject.index} / {String(projects.length).padStart(2, '0')}</span>
      </motion.header>

      <AnimatePresence>
        {menuOpen && <motion.nav id="reel-mobile-nav" className={styles.mobileNav} aria-label="Project navigation" initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }} transition={{ duration: 0.2 }}>
          {projects.map((project, index) => <button key={project.id} type="button" onClick={() => goToProject(index)}><span>{project.index}</span>{project.title.replace('\n', ' ')}</button>)}
        </motion.nav>}
      </AnimatePresence>

      <section className={styles.sequence} ref={sequenceRef} aria-label="Selected architectural projects">
        <div className={styles.sequencePin} ref={pinRef}>
          {projects.map((project, index) => (
            <ProjectBody
              key={project.id}
              project={project}
              index={index}
              eagerMedia={wideViewport}
            />
          ))}
        </div>
      </section>
      <div className={styles.progress} aria-hidden="true"><span style={{ transform: `scaleY(${(activeIndex + 1) / projects.length})` }} /></div>
    </main>
  );
}
