import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, ArrowUpRight, MapPin, ShieldCheck } from 'lucide-react';
import { crmGet } from '../state/useCrmApi';
import styles from './SitePreview.module.css';

function readRoute() {
  const marker = '/site-preview/';
  const slug = decodeURIComponent(window.location.pathname.slice(marker.length)).replace(/^\/+|\/+$/g, '');
  const revision = new URLSearchParams(window.location.search).get('revision');
  return { slug, revision };
}

function object(value, fallback = {}) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : fallback;
}

function cleanAreas(content) {
  const sourceBacked = Array.isArray(content.areas)
    ? content.areas.map((area) => area?.name).filter(Boolean)
    : [];
  if (sourceBacked.length) return { values: sourceBacked, verified: true };
  const requested = Array.isArray(content.requested_service_areas)
    ? content.requested_service_areas.filter(Boolean)
    : [];
  return { values: requested, verified: false };
}

function PreviewMedia({ src }) {
  const videoRef = useRef(null);
  const [reducedMotion, setReducedMotion] = useState(
    () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  );
  const saveData = Boolean(navigator.connection?.saveData);
  const estateMedia = src === '/media/mountain-waterfall-estate-v1.webp';
  const useVideo = estateMedia && !reducedMotion && !saveData;

  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setReducedMotion(query.matches);
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !useVideo) return undefined;
    const sync = () => {
      if (document.hidden) video.pause();
      else void video.play().catch(() => {});
    };
    document.addEventListener('visibilitychange', sync);
    video.addEventListener('canplay', sync);
    sync();
    return () => {
      document.removeEventListener('visibilitychange', sync);
      video.removeEventListener('canplay', sync);
      video.pause();
    };
  }, [useVideo]);

  if (useVideo) {
    return (
      <video ref={videoRef} poster={src} autoPlay muted loop playsInline preload="metadata" aria-hidden="true" tabIndex={-1} disablePictureInPicture>
        <source src="/media/mountain-waterfall-estate-v1-mobile.mp4" type="video/mp4" media="(max-width: 759px)" />
        <source src="/media/mountain-waterfall-estate-v1.mp4" type="video/mp4" />
      </video>
    );
  }
  return <img src={src} alt="Architectural property in a mountain landscape" fetchPriority="high" />;
}

async function loadPreview(slug, revisionId) {
  const list = await crmGet('/api/sites?limit=100');
  const site = (Array.isArray(list?.sites) ? list.sites : []).find((item) => item.slug === slug);
  if (!site) throw new Error('This private site preview was not found in your workspace.');
  if (!revisionId || site.preview_revision_id === revisionId) return site;

  const detail = await crmGet(`/api/sites/${encodeURIComponent(site.id)}`);
  const revision = (Array.isArray(detail?.revisions) ? detail.revisions : [])
    .find((item) => item.id === revisionId);
  if (!revision) throw new Error('That private revision is no longer available.');
  return { ...site, ...revision, preview_revision_id: revision.id };
}

export function SitePreview() {
  const [{ slug, revision }] = useState(readRoute);
  const [site, setSite] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let live = true;
    loadPreview(slug, revision).then(
      (result) => { if (live) setSite(result); },
      (reason) => { if (live) setError(reason?.message || 'The private preview could not be loaded.'); },
    );
    return () => { live = false; };
  }, [revision, slug]);

  const content = object(site?.content);
  const theme = object(site?.brand_theme);
  const areas = useMemo(() => cleanAreas(content), [content]);
  const authorizedFeeds = Array.isArray(site?.authorized_idx_sources) ? site.authorized_idx_sources : [];
  const media = content.hero_media_url || '/media/mountain-waterfall-estate-v1.webp';
  const brand = content.public_brand_name || site?.name || 'Local real estate';

  useLayoutEffect(() => {
    document.documentElement.dataset.sitePreview = 'true';
    return () => { delete document.documentElement.dataset.sitePreview; };
  }, []);

  if (error) {
    return (
      <main className={styles.state}>
        <span>Private preview</span>
        <h1>Preview unavailable</h1>
        <p role="alert">{error}</p>
        <a href="/"><ArrowLeft aria-hidden="true" /> Return to Neoh</a>
      </main>
    );
  }

  if (!site) {
    return <main className={styles.state} role="status"><span>Private preview</span><h1>Preparing your site</h1><p>Loading the exact saved revision…</p></main>;
  }

  const tokens = {
    '--preview-bg': theme.background || '#171612',
    '--preview-surface': theme.surface || '#2B2922',
    '--preview-text': theme.text || '#F2F2F2',
    '--preview-muted': theme.muted || '#ABABAB',
    '--preview-accent': theme.accent || '#FFBC1F',
    '--preview-border': theme.border || '#8A7550',
  };

  return (
    <main className={styles.preview} style={tokens}>
      <div className={styles.previewBar} role="status">
        <span><ShieldCheck aria-hidden="true" /> Private preview · revision {site.revision || '1'}</span>
        <a href="/"><ArrowLeft aria-hidden="true" /> Back to Our AI</a>
      </div>

      <section className={styles.hero} aria-labelledby="preview-headline">
        <PreviewMedia src={media} />
        <div className={styles.scrim} aria-hidden="true" />
        <header className={styles.navigation}>
          <strong>{brand}</strong>
          <span>{content.agent_name || 'Independent real-estate guidance'}</span>
        </header>
        <div className={styles.heroCopy}>
          <span>{content.eyebrow || 'LOCAL REAL ESTATE'}</span>
          <h1 id="preview-headline">{content.headline || site.name}</h1>
          <p>{content.description}</p>
          <a href="#areas">Explore the area <ArrowUpRight aria-hidden="true" /></a>
        </div>
        <aside className={styles.trust} aria-label="Agent information">
          <span>Direct guidance</span>
          <strong>{content.agent_name || 'Agent name pending'}</strong>
          <small>{content.license_number ? `License ${content.license_number}` : 'License details pending review'}</small>
        </aside>
      </section>

      <section id="areas" className={styles.areas} aria-labelledby="areas-title">
        <header>
          <span>Service area</span>
          <h2 id="areas-title">Local, focused, accountable.</h2>
          <p>{areas.verified ? 'Area descriptions are backed by the sources saved with this revision.' : 'These requested areas stay unpublished until their local claims and sources are reviewed.'}</p>
        </header>
        <ul>
          {areas.values.length ? areas.values.map((area) => (
            <li key={area}><MapPin aria-hidden="true" /><strong>{area}</strong><small>{areas.verified ? 'Source-backed guide' : 'Source review pending'}</small></li>
          )) : <li><MapPin aria-hidden="true" /><strong>Area guide pending</strong><small>Add a source-backed service area in Our AI</small></li>}
        </ul>
      </section>

      <section className={styles.intake} aria-labelledby="intake-title">
        <span>{authorizedFeeds.length ? 'AUTHORIZED LISTING ACCESS' : 'DIRECT AGENT INTAKE'}</span>
        <h2 id="intake-title">A shorter path to the right next step.</h2>
        <p>{authorizedFeeds.length ? `${authorizedFeeds.length} authorized IDX source${authorizedFeeds.length === 1 ? '' : 's'} attached to this revision.` : 'No listing feed is represented until an authorized IDX source is connected.'}</p>
        <div><strong>01</strong><span>Tell us your goal</span><strong>02</strong><span>Answer three focused questions</span><strong>03</strong><span>Continue with your agent</span></div>
      </section>

      <footer className={styles.footer}>
        <strong>{brand}</strong>
        <span>{content.seo_description || 'Local real-estate guidance.'}</span>
        <small>Private Neoh preview · not a published website</small>
      </footer>
    </main>
  );
}
