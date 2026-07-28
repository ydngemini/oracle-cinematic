import { useEffect, useRef, useState } from 'react';
import styles from './ReelExperience.module.css';

const projects = [
  {
    id: 'fall-line-house',
    index: '01',
    eyebrow: 'Residential / Ridge Line',
    title: 'Fall Line\nHouse',
    subtitle: 'A concrete residence held between the contour of the land and the evening horizon.',
    location: 'High Desert, Utah',
    year: '2026',
    typology: 'Private residence',
    area: '3,840 sq ft',
    accent: 'bronze',
    src: '/projects/fall-line-house/hero.webp',
    alt: 'Concrete residence on a ridgeline at blue hour with warm interior lighting',
  },
  {
    id: 'threshold-water-side',
    index: '02',
    eyebrow: 'Hospitality / Water Edge',
    title: 'Threshold /\nWater Side',
    subtitle: 'A quiet pavilion where shadow, reflection, and the shoreline set the pace of arrival.',
    location: 'Great Lakes, Michigan',
    year: '2026',
    typology: 'Retreat pavilion',
    area: '2,190 sq ft',
    accent: 'amber',
    src: '/projects/threshold-water-side/hero.webp',
    alt: 'Modern lakeside pavilion at dawn reflected in dark water',
  },
  {
    id: 'timber-core',
    index: '03',
    eyebrow: 'Interior / Material Study',
    title: 'Timber\nCore',
    subtitle: 'An interior organized around a single oak volume, with concrete planes and low amber light.',
    location: 'Chicago, Illinois',
    year: '2026',
    typology: 'Adaptive interior',
    area: '5,120 sq ft',
    accent: 'concrete',
    src: '/projects/timber-core/hero.webp',
    alt: 'Dark architectural interior featuring a monumental oak core and concrete planes',
  },
];

function scrollToProject(id, reducedMotion) {
  document.getElementById(id)?.scrollIntoView({
    behavior: reducedMotion ? 'auto' : 'smooth',
    block: 'start',
  });
}

export function ReelBackdrop() {
  return (
    <div className={styles.backdrop} aria-hidden="true">
      <img src="/architectural-waterfall.webp" alt="" />
      <span className={styles.waterfallFlow} />
    </div>
  );
}

export function ReelExperience() {
  const [activeIndex, setActiveIndex] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);
  const menuTriggerRef = useRef(null);
  const menuWasOpen = useRef(false);

  useEffect(() => {
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
    const syncPreference = () => setReducedMotion(preference.matches);
    syncPreference();
    preference.addEventListener('change', syncPreference);
    return () => preference.removeEventListener('change', syncPreference);
  }, []);

  useEffect(() => {
    if (menuWasOpen.current && !menuOpen) menuTriggerRef.current?.focus();
    menuWasOpen.current = menuOpen;
  }, [menuOpen]);

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (visible) setActiveIndex(Number(visible.target.dataset.projectIndex));
      },
      { threshold: [0.35, 0.55, 0.75] },
    );
    const stages = document.querySelectorAll('[data-reel-project]');
    stages.forEach((stage) => observer.observe(stage));
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const closeOnEscape = (event) => {
      if (event.key === 'Escape') setMenuOpen(false);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, []);

  const activeProject = projects[activeIndex];

  return (
    <main className={styles.reel} id="reel-main">
      <a className={styles.skipLink} href="#fall-line-house">Skip to project sequence</a>
      <header className={styles.header}>
        <a className={styles.brand} href="/" aria-label="Return to Neoh">
          <span>Neoh</span><small>Architectural reel</small>
        </a>
        <nav className={styles.desktopNav} aria-label="Project navigation">
          {projects.map((project) => (
            <button
              className={activeProject.id === project.id ? styles.activeNav : undefined}
              key={project.id}
              type="button"
              onClick={() => scrollToProject(project.id, reducedMotion)}
            >
              {project.index} / {project.title.replace('\n', ' ')}
            </button>
          ))}
        </nav>
        <button
          type="button"
          ref={menuTriggerRef}
          className={styles.menuButton}
          aria-expanded={menuOpen}
          aria-controls="reel-mobile-nav"
          onClick={() => setMenuOpen((open) => !open)}
        >
          {menuOpen ? 'Close' : 'Projects'}
        </button>
        <span className={styles.counter} aria-live="polite">{activeProject.index} / 03</span>
      </header>

      <nav className={`${styles.mobileNav} ${menuOpen ? styles.mobileNavOpen : ''}`} id="reel-mobile-nav" aria-label="Project navigation" hidden={!menuOpen}>
        {projects.map((project) => (
          <button
            key={project.id}
            type="button"
            onClick={() => {
              scrollToProject(project.id, reducedMotion);
              setMenuOpen(false);
            }}
          >
            <span>{project.index}</span>{project.title.replace('\n', ' ')}
          </button>
        ))}
      </nav>

      <section className={styles.sequence} aria-label="Selected architectural projects">
        {projects.map((project, index) => (
          <article
            className={`${styles.project} ${styles[`accent${project.accent}`]}`}
            data-project-index={index}
            data-reel-project
            id={project.id}
            key={project.id}
          >
            <div className={styles.projectRail}>
              <p className={styles.eyebrow}><span>{project.index}</span>{project.eyebrow}</p>
              <h1>{project.title.split('\n').map((line) => <span key={line}>{line}</span>)}</h1>
              <p className={styles.description}>{project.subtitle}</p>
            </div>
            <figure className={styles.mediaStage}>
              <img src={project.src} alt={project.alt} loading={index === 0 ? 'eager' : 'lazy'} />
              <figcaption>Generated architectural study · approval media pending</figcaption>
            </figure>
            <dl className={styles.metadata}>
              <div><dt>Location</dt><dd>{project.location}</dd></div>
              <div><dt>Completion</dt><dd>{project.year}</dd></div>
              <div><dt>Program</dt><dd>{project.typology}</dd></div>
              <div><dt>Area</dt><dd>{project.area}</dd></div>
            </dl>
          </article>
        ))}
      </section>
      <div className={styles.progress} aria-hidden="true"><span style={{ transform: `scaleY(${(activeIndex + 1) / projects.length})` }} /></div>
    </main>
  );
}
