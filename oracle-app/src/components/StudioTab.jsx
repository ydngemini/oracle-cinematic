import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Clapperboard,
  ExternalLink,
  Globe2,
  MonitorSmartphone,
  Plus,
  RefreshCw,
  X,
} from 'lucide-react';
import { crmGet, crmPost } from '../state/useCrmApi';
// Nine sites_api routes had no caller: Studio could draft and preview a site and
// then stopped, so nothing could actually be published, shared, or measured.
import SitePublishPanel from './SitePublishPanel';
import { PanelDataStatus } from './PanelDataStatus';
import styles from './StudioTab.module.css';

const VideoStudioPanel = lazy(() => import('./VideoStudioPanel'));

const STUDIO_VIEWS = [
  { id: 'sites', name: 'Sites', icon: Globe2 },
  { id: 'video', name: 'Video', icon: Clapperboard },
];

const TEMPLATES = [
  { id: 'editorial', name: 'Editorial', detail: 'Asymmetric media and quiet typography' },
  { id: 'neighborhood', name: 'Neighborhood', detail: 'Area guides and source-backed local pages' },
  { id: 'listing_focus', name: 'Listing', detail: 'Property-first search and conversion' },
];

const STEPS = ['Template', 'Brand', 'Coverage', 'Trust', 'Publish'];

const EMPTY_DRAFT = {
  template_key: 'editorial',
  site_name: '',
  brand_name: '',
  hero_headline: '',
  hero_subheadline: '',
  service_areas: '',
  idx_enabled: false,
  agent_name: '',
  license_number: '',
  bio: '',
  domain: '',
  seo_title: '',
  seo_description: '',
  website_chat_intake: 'buyer_three_question',
};

function normalizeSites(payload) {
  if (Array.isArray(payload?.sites)) return payload.sites;
  if (Array.isArray(payload?.items)) return payload.items;
  return [];
}

function siteLabel(site) {
  return site.name || site.site_name || site.brand?.name || site.domain || 'Untitled site';
}

function siteStatus(site) {
  return String(site.status || 'draft').replaceAll('_', ' ');
}

function splitAreas(value) {
  return value.split(',').map((area) => area.trim()).filter(Boolean);
}

function siteSlug(value) {
  const normalized = value
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 63)
    .replace(/-+$/g, '');
  const candidate = normalized.length >= 3 ? normalized : `${normalized || 'new'}-site`;
  return candidate.slice(0, 63).replace(/-+$/g, '');
}

function previewUrl(site) {
  if (site.preview_url || site.url || site.published_url) {
    return site.preview_url || site.url || site.published_url;
  }
  if (site.slug && site.preview_revision_id) {
    return `/site-preview/${site.slug}?revision=${site.preview_revision_id}`;
  }
  return '';
}

export default function StudioTab({ embedded = false }) {
  const [view, setView] = useState('sites');
  const [sites, setSites] = useState(null);
  const [managingSiteId, setManagingSiteId] = useState(null);
  const [sitesError, setSitesError] = useState(null);
  const [sitesRefreshing, setSitesRefreshing] = useState(false);
  const [sitesUpdatedAt, setSitesUpdatedAt] = useState(null);
  const [idx, setIdx] = useState(null);
  const [idxError, setIdxError] = useState(null);
  const [idxUpdatedAt, setIdxUpdatedAt] = useState(null);
  const [creating, setCreating] = useState(false);
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState(false);

  const loadSites = useCallback(() => {
    setSitesRefreshing(true);
    return crmGet('/api/sites').then(
      (payload) => {
        setSites(normalizeSites(payload));
        setSitesError(null);
        setSitesRefreshing(false);
        setSitesUpdatedAt(new Date());
      },
      (error) => {
        setSitesError(error);
        setSitesRefreshing(false);
      },
    );
  }, []);

  const loadIdx = useCallback(() => crmGet('/api/mls/health').then(
    (payload) => {
      setIdx(payload || {});
      setIdxError(null);
      setIdxUpdatedAt(new Date());
    },
    (error) => setIdxError(error),
  ), []);

  useEffect(() => {
    const initial = Promise.allSettled([
      Promise.resolve().then(loadSites),
      Promise.resolve().then(loadIdx),
    ]);
    return () => { void initial; };
  }, [loadIdx, loadSites]);

  const setField = (name, value) => setDraft((current) => ({ ...current, [name]: value }));
  const closeWizard = () => {
    if (saving) return;
    setCreating(false);
    setStep(0);
    setSaveError('');
  };

  const idxConnected = useMemo(() => {
    const status = String(idx?.status || idx?.connection_status || '').toLowerCase();
    const providers = Array.isArray(idx?.sources) ? idx.sources : Array.isArray(idx?.providers) ? idx.providers : [];
    return ['active', 'connected', 'healthy', 'ready'].includes(status)
      || providers.some((provider) => ['active', 'connected', 'healthy', 'ready'].includes(String(provider.health || provider.status || '').toLowerCase()));
  }, [idx]);

  const stepValid = useMemo(() => {
    if (step === 0) return Boolean(draft.template_key);
    if (step === 1) return Boolean(draft.site_name.trim() && draft.brand_name.trim() && draft.hero_headline.trim());
    if (step === 2) return splitAreas(draft.service_areas).length > 0 && (!draft.idx_enabled || idxConnected);
    if (step === 3) return Boolean(draft.agent_name.trim());
    return Boolean(draft.seo_title.trim());
  }, [draft, idxConnected, step]);

  const saveDraft = async () => {
    if (!stepValid || saving) return;
    setSaving(true);
    setSaveError('');
    const description = draft.hero_subheadline.trim().length >= 20
      ? draft.hero_subheadline.trim()
      : 'A direct path to trusted local guidance, verified listings, and a conversation with your agent.';
    const seoDescription = draft.seo_description.trim().length >= 20
      ? draft.seo_description.trim()
      : description.slice(0, 170);
    const payload = {
      name: draft.site_name.trim(),
      slug: siteSlug(draft.site_name),
      template_key: draft.template_key,
      brand_theme: {
        background: '#171612',
        surface: '#2B2922',
        text: '#F2F2F2',
        muted: '#ABABAB',
        accent: '#FFBC1F',
        border: '#8A7550',
        glass_opacity: 0.2,
        font_pair: 'narrow_editorial',
      },
      headline: draft.hero_headline.trim(),
      content: {
        eyebrow: 'LOCAL REAL ESTATE',
        headline: draft.hero_headline.trim(),
        description,
        hero_media_url: '/media/mountain-waterfall-estate-v1.webp',
        public_brand_name: draft.brand_name.trim(),
        agent_name: draft.agent_name.trim(),
        license_number: draft.license_number.trim(),
        agent_bio: draft.bio.trim(),
        requested_service_areas: splitAreas(draft.service_areas),
        idx_requested: draft.idx_enabled,
        requested_domain: draft.domain.trim() || null,
        areas: [],
        seo_title: draft.seo_title.trim(),
        seo_description: seoDescription,
        website_chat_intake: draft.website_chat_intake,
      },
      authorized_idx_sources: [],
    };

    try {
      const result = await crmPost('/api/sites', payload);
      let saved = result?.site || result;
      if (saved?.id && result?.revision?.id) {
        try {
          const preview = await crmPost(`/api/sites/${encodeURIComponent(saved.id)}/preview`, {
            revision_id: result.revision.id,
          });
          saved = {
            ...(preview?.site || saved),
            content: result.revision.content,
            preview_url: preview?.preview_path || '',
          };
        } catch {
          saved = { ...saved, content: result.revision.content };
        }
      }
      if (saved && typeof saved === 'object' && saved.id) {
        setSites((current) => [saved, ...(current || []).filter((site) => site.id !== saved.id)]);
        setSitesUpdatedAt(new Date());
      } else {
        await loadSites();
      }
      setDraft(EMPTY_DRAFT);
      setStep(0);
      setCreating(false);
    } catch (error) {
      setSaveError(error?.message || 'The site draft could not be saved.');
    } finally {
      setSaving(false);
    }
  };

  const openSite = (site) => {
    const url = previewUrl(site);
    if (!url) return;
    window.open(url, '_blank', 'noopener,noreferrer');
  };

  return (
    <section className={styles.wrap} aria-labelledby={embedded ? 'studio-sites-title' : 'studio-title'}>
      <nav className={styles.viewSwitch} aria-label="Studio workspace">
        {STUDIO_VIEWS.map(({ id, name, icon: Icon }) => (
          <button
            key={id}
            type="button"
            aria-current={view === id ? 'page' : undefined}
            data-active={view === id}
            onClick={() => setView(id)}
          >
            <Icon aria-hidden="true" /> {name}
          </button>
        ))}
      </nav>

      {view === 'video' ? (
        <Suspense fallback={<div className={styles.viewLoading} role="status">Loading video studio…</div>}>
          <VideoStudioPanel />
        </Suspense>
      ) : (
        <>

      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>{embedded ? 'Hyperlocal presence' : 'AI growth workspace'}</span>
          <h1 id={embedded ? 'studio-sites-title' : 'studio-title'}>{embedded ? 'Sites & IDX' : 'Our AI'}</h1>
          <p>Build a source-backed local website with NEOH and preview every change before publishing.</p>
        </div>
        <div className={styles.heroActions}>
          <button type="button" className={styles.iconButton} onClick={loadSites} disabled={sitesRefreshing} aria-label="Refresh sites">
            <RefreshCw aria-hidden="true" />
          </button>
          <button type="button" className={styles.primary} onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" /> New site
          </button>
        </div>
      </header>

      <section className={styles.sourceRail} aria-labelledby="studio-source-title">
        <h2 id="studio-source-title" className={styles.srOnly}>Our AI source status</h2>
        <ul>
          <PanelDataStatus
            label="Websites"
            loading={sites === null && !sitesError}
            refreshing={sitesRefreshing}
            error={sitesError}
            updatedAt={sitesUpdatedAt}
            onRetry={loadSites}
          />
          <PanelDataStatus
            label="MLS / IDX authorization"
            loading={idx === null && !idxError}
            refreshing={false}
            error={idxError}
            updatedAt={idxUpdatedAt}
            onRetry={loadIdx}
          />
        </ul>
      </section>

      <section className={styles.siteList} aria-labelledby="sites-title">
        <header>
          <div>
            <span className={styles.kicker}>Owned presence</span>
            <h2 id="sites-title">Websites</h2>
          </div>
          <span>{sites?.length ?? '—'}</span>
        </header>
        {sites === null && !sitesError ? (
          <div className={styles.skeleton} aria-hidden="true"><span /><span /></div>
        ) : sites?.length ? (
          <ul>
            {sites.map((site, index) => {
              const hasUrl = Boolean(previewUrl(site));
              return (
                <li key={site.id || `${siteLabel(site)}:${index}`}>
                  <Globe2 aria-hidden="true" />
                  <div>
                    <strong>{siteLabel(site)}</strong>
                    <small>{site.content?.requested_domain || site.domain || site.preview_domain || (site.slug ? `${site.slug} · private preview` : 'Preview domain pending')}</small>
                  </div>
                  <span data-status={site.status || 'draft'}>{siteStatus(site)}</span>
                  <button type="button" disabled={!hasUrl} onClick={() => openSite(site)}>
                    Preview <ExternalLink aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    onClick={() => setManagingSiteId((current) => (current === site.id ? null : site.id))}
                    aria-expanded={managingSiteId === site.id}
                    disabled={!site.id}
                  >
                    {managingSiteId === site.id ? 'Close' : 'Publish & access'}
                  </button>
                  {managingSiteId === site.id ? <SitePublishPanel siteId={site.id} /> : null}
                </li>
              );
            })}
          </ul>
        ) : !sitesError ? (
          <div className={styles.empty} role="status">
            <MonitorSmartphone aria-hidden="true" />
            <div><strong>No website drafts yet</strong><p>Start with one template and publish only after desktop and mobile review.</p></div>
            <button type="button" onClick={() => setCreating(true)}>Create first site</button>
          </div>
        ) : null}
      </section>

      {creating ? (
        <section className={styles.builder} aria-labelledby="builder-title">
          <header className={styles.builderHead}>
            <div>
              <span className={styles.kicker}>Step {step + 1} of {STEPS.length}</span>
              <h2 id="builder-title">{STEPS[step]}</h2>
            </div>
            <button type="button" className={styles.close} onClick={closeWizard} aria-label="Close website builder"><X aria-hidden="true" /></button>
          </header>

          <ol className={styles.steps} aria-label="Website setup progress">
            {STEPS.map((name, index) => (
              <li key={name} aria-current={index === step ? 'step' : undefined} data-complete={index < step}>
                <span>{index < step ? <Check aria-hidden="true" /> : index + 1}</span>
                <small>{name}</small>
              </li>
            ))}
          </ol>

          <div className={styles.builderBody}>
            <form className={styles.form} onSubmit={(event) => event.preventDefault()}>
              {step === 0 ? (
                <fieldset className={styles.templates}>
                  <legend>Choose a starting structure</legend>
                  {TEMPLATES.map((template) => (
                    <label key={template.id}>
                      <input type="radio" name="template" value={template.id} checked={draft.template_key === template.id} onChange={() => setField('template_key', template.id)} />
                      <span><strong>{template.name}</strong><small>{template.detail}</small></span>
                    </label>
                  ))}
                </fieldset>
              ) : null}

              {step === 1 ? (
                <fieldset>
                  <legend>Brand and hero</legend>
                  <label><span>Internal site name</span><input value={draft.site_name} onChange={(event) => setField('site_name', event.target.value)} placeholder="Chicago seller site" required /></label>
                  <label><span>Public brand name</span><input value={draft.brand_name} onChange={(event) => setField('brand_name', event.target.value)} placeholder="Your brokerage or team" required /></label>
                  <label><span>Hero headline</span><input value={draft.hero_headline} onChange={(event) => setField('hero_headline', event.target.value)} placeholder="A clear promise for your market" required /></label>
                  <label><span>Hero support line</span><textarea value={draft.hero_subheadline} onChange={(event) => setField('hero_subheadline', event.target.value)} rows={3} placeholder="What a visitor can do next" /></label>
                </fieldset>
              ) : null}

              {step === 2 ? (
                <fieldset>
                  <legend>Coverage and listing authorization</legend>
                  <label><span>Service areas</span><input value={draft.service_areas} onChange={(event) => setField('service_areas', event.target.value)} placeholder="Chicago, Oak Park, Evanston" required /><small>Separate cities, neighborhoods, or ZIPs with commas.</small></label>
                  <label className={styles.checkRow}>
                    <input type="checkbox" checked={draft.idx_enabled} disabled={!idxConnected} onChange={(event) => setField('idx_enabled', event.target.checked)} />
                    <span><strong>Include authorized IDX search</strong><small>{idxConnected ? 'Connected provider will remain source-attributed.' : 'Connect an authorized MLS provider before enabling.'}</small></span>
                  </label>
                  <div className={styles.intakeChoice} role="group" aria-label="Website chat intake">
                    <span>Website chat intake</span>
                    <label><input type="radio" name="intake" checked={draft.website_chat_intake === 'buyer_three_question'} onChange={() => setField('website_chat_intake', 'buyer_three_question')} /> Buyer · budget, beds/baths, area</label>
                    <label><input type="radio" name="intake" checked={draft.website_chat_intake === 'seller_three_question'} onChange={() => setField('website_chat_intake', 'seller_three_question')} /> Seller · address, timeline, outcome</label>
                  </div>
                </fieldset>
              ) : null}

              {step === 3 ? (
                <fieldset>
                  <legend>Agent trust details</legend>
                  <label><span>Agent or team name</span><input value={draft.agent_name} onChange={(event) => setField('agent_name', event.target.value)} required /></label>
                  <label><span>License number</span><input value={draft.license_number} onChange={(event) => setField('license_number', event.target.value)} /></label>
                  <label><span>Short bio</span><textarea value={draft.bio} onChange={(event) => setField('bio', event.target.value)} rows={5} placeholder="Your verified experience and service promise" /></label>
                </fieldset>
              ) : null}

              {step === 4 ? (
                <fieldset>
                  <legend>Domain, SEO, and draft review</legend>
                  <label><span>Requested domain</span><input value={draft.domain} onChange={(event) => setField('domain', event.target.value)} inputMode="url" placeholder="homes.example.com" /></label>
                  <label><span>Search title</span><input value={draft.seo_title} onChange={(event) => setField('seo_title', event.target.value)} maxLength={70} required /></label>
                  <label><span>Search description</span><textarea value={draft.seo_description} onChange={(event) => setField('seo_description', event.target.value)} rows={4} maxLength={170} /></label>
                  <p className={styles.disclosure}>Saving creates a private, reversible draft. Publishing, domains, ads, and external spend remain separate approval steps.</p>
                </fieldset>
              ) : null}
            </form>

            <aside className={styles.preview} aria-label="Website preview">
              <span className={styles.previewChrome}><i /><i /><i /><small>Private preview</small></span>
              <div className={styles.previewMedia}>
                <small>{draft.brand_name || 'Brand preview'}</small>
                <strong>{draft.hero_headline || 'Your headline appears here'}</strong>
                <p>{draft.hero_subheadline || 'A focused next step for buyers and sellers.'}</p>
                <span>{splitAreas(draft.service_areas).slice(0, 2).join(' · ') || 'Service area'}</span>
              </div>
            </aside>
          </div>

          {saveError ? <p className={styles.saveError} role="alert">{saveError}</p> : null}
          <footer className={styles.builderActions}>
            <button type="button" className={styles.secondary} disabled={step === 0 || saving} onClick={() => setStep((current) => current - 1)}><ArrowLeft aria-hidden="true" /> Back</button>
            {step < STEPS.length - 1 ? (
              <button type="button" className={styles.primary} disabled={!stepValid} onClick={() => setStep((current) => current + 1)}>Continue <ArrowRight aria-hidden="true" /></button>
            ) : (
              <button type="button" className={styles.primary} disabled={!stepValid || saving} onClick={saveDraft}>{saving ? 'Saving…' : 'Save private draft'} <Check aria-hidden="true" /></button>
            )}
          </footer>
        </section>
      ) : null}
        </>
      )}
    </section>
  );
}
