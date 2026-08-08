import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Check,
  Clapperboard,
  ImagePlus,
  Loader2,
  Play,
  RefreshCw,
  Trash2,
  Type,
  Wand2,
  X,
} from 'lucide-react';
import { crmGet, crmDelete, crmPost } from '../state/useCrmApi';
import useProtectedMedia from '../state/useProtectedMedia';
import styles from './VideoStudioPanel.module.css';

const ACTIVE_STATUSES = ['queued', 'scripting', 'generating', 'stitching'];
const POLL_MS = 5000;

const STATUS_LABELS = {
  queued: 'Queued',
  scripting: 'Writing script',
  generating: 'Generating video',
  stitching: 'Stitching clips',
  ready: 'Ready',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

function statusLabel(job) {
  return STATUS_LABELS[job?.status] || String(job?.status || 'unknown').replaceAll('_', ' ');
}

function normalizeJobs(payload) {
  if (Array.isArray(payload?.jobs)) return payload.jobs;
  return [];
}

export default function VideoStudioPanel() {
  const [jobs, setJobs] = useState(null);
  const [jobsError, setJobsError] = useState(null);
  const [config, setConfig] = useState(null);
  const [configError, setConfigError] = useState(null);

  const [creating, setCreating] = useState(false);
  const [kind, setKind] = useState('text');
  const [size, setSize] = useState('');
  const [leadId, setLeadId] = useState('');
  const [listingId, setListingId] = useState('');
  const [address, setAddress] = useState('');
  const [voiceover, setVoiceover] = useState(false);
  // Default on: reels autoplay muted, so narration that lives only in the audio
  // track reaches nobody.
  const [captions, setCaptions] = useState(true);
  const [script, setScript] = useState('');
  const [draftingScript, setDraftingScript] = useState(false);
  const [photos, setPhotos] = useState(null);
  const [selectedPhotos, setSelectedPhotos] = useState([]);
  const [submitError, setSubmitError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [created, setCreated] = useState(null);

  const pollTimer = useRef(null);
  const loadJobs = useCallback(() => {
    return crmGet('/api/video-studio/jobs').then(
      (payload) => {
        setJobs(normalizeJobs(payload));
        setJobsError(null);
      },
      (error) => setJobsError(error),
    );
  }, []);

  const loadConfig = useCallback(() => {
    return crmGet('/api/video-studio/config').then(
      (payload) => {
        setConfig(payload);
        setConfigError(null);
      },
      (error) => setConfigError(error),
    );
  }, []);

  const loadPhotos = useCallback((anchor = {}) => {
    const lead = anchor.leadId ?? leadId;
    const listing = anchor.listingId ?? listingId;
    if (!lead && !listing) {
      setPhotos([]);
      return Promise.resolve();
    }
    const params = new URLSearchParams();
    if (lead) params.set('lead_id', lead);
    if (listing) params.set('listing_id', listing);
    return crmGet(`/api/crm/media?${params.toString()}`).then(
      (payload) => setPhotos(payload?.media || []),
      () => setPhotos([]),
    );
  }, [leadId, listingId]);

  useEffect(() => {
    loadJobs();
    loadConfig();
  }, [loadJobs, loadConfig]);

  // Load the property photo filmstrip whenever the anchor changes.
  useEffect(() => {
    const debounce = window.setTimeout(() => loadPhotos(), 350);
    return () => window.clearTimeout(debounce);
  }, [leadId, listingId, loadPhotos]);

  // Poll while any job is still active; stop as soon as all are terminal.
  useEffect(() => {
    const active = (jobs || []).some((job) => ACTIVE_STATUSES.includes(job.status));
    if (!active) {
      if (pollTimer.current) {
        window.clearTimeout(pollTimer.current);
        pollTimer.current = null;
      }
      return undefined;
    }
    pollTimer.current = window.setTimeout(loadJobs, POLL_MS);
    return () => {
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
    };
  }, [jobs, loadJobs]);

  // Photo filmstrip resolves through the authenticated media endpoint.
  const displayPhotos = useProtectedMedia(photos || [], {});

  // Finished videos resolve to object URLs for <video> preview.
  const readyMedia = useMemo(
    () => (jobs || [])
      .filter((job) => job.status === 'ready' && job.media_id)
      .map((job) => ({ id: job.media_id, url: `/api/media/${job.media_id}` })),
    [jobs],
  );
  const displayMedia = useProtectedMedia(readyMedia, {});

  const togglePhoto = (id) => {
    const max = config?.max_images || 4;
    setSelectedPhotos((current) => {
      if (current.includes(id)) return current.filter((value) => value !== id);
      if (current.length >= max) return current;
      return [...current, id];
    });
  };

  const draftScript = () => {
    if (draftingScript) return;
    setDraftingScript(true);
    crmPost('/api/video-studio/script', { property: { address } })
      .then((payload) => {
        if (payload?.script) setScript(payload.script);
      })
      .catch((error) => setSubmitError(error?.message || 'Script drafting failed.'))
      .finally(() => setDraftingScript(false));
  };

  const resetForm = () => {
    setCreating(false);
    setKind('text');
    setSize('');
    setLeadId('');
    setListingId('');
    setAddress('');
    setVoiceover(false);
    setScript('');
    setSelectedPhotos([]);
    setPhotos(null);
    setSubmitError('');
    setCreated(null);
  };

  const submitJob = () => {
    if (submitting || !size) return;
    setSubmitting(true);
    setSubmitError('');
    const body = {
      kind,
      size,
      voiceover,
      captions,
      script: script.trim() || null,
      property: { address },
    };
    if (leadId.trim()) body.lead_id = leadId.trim();
    if (listingId.trim()) body.listing_id = listingId.trim();
    if (kind === 'image') body.images = selectedPhotos;

    crmPost('/api/video-studio/jobs', body)
      .then((job) => {
        setCreated(job);
        setJobs((current) => [job, ...(current || [])]);
        loadJobs();
        setSubmitting(false);
      })
      .catch((error) => {
        const detail = error?.detail;
        const message = typeof detail === 'object' && detail?.message
          ? detail.message
          : (error?.message || 'The video job could not be created.');
        setSubmitError(message);
        setSubmitting(false);
      });
  };

  const cancelJob = (job) => {
    crmPost(`/api/video-studio/jobs/${encodeURIComponent(job.id)}/cancel`)
      .then(() => loadJobs())
      .catch(() => loadJobs());
  };

  const deleteJob = (job) => {
    crmDelete(`/api/video-studio/jobs/${encodeURIComponent(job.id)}`)
      .then(() => loadJobs())
      .catch(() => loadJobs());
  };

  const activeCount = (jobs || []).filter((job) => ACTIVE_STATUSES.includes(job.status)).length;
  const consumed = config?.consumed_seconds ?? 0;
  const quota = config?.daily_quota_seconds ?? 0;

  return (
    <section className={styles.wrap} aria-labelledby="video-studio-title">
      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>Sora 2 marketing engine</span>
          <h1 id="video-studio-title">Video Studio</h1>
          <p>Turn property data and photos into a scripted, narrated marketing reel.</p>
        </div>
        <div className={styles.heroActions}>
          <button type="button" className={styles.iconButton} onClick={loadJobs} aria-label="Refresh video jobs">
            <RefreshCw aria-hidden="true" />
          </button>
          <button type="button" className={styles.primary} onClick={() => setCreating(true)}>
            <Clapperboard aria-hidden="true" /> New reel
          </button>
        </div>
      </header>

      {quota > 0 ? (
        <p className={styles.quota} role="status">
          Today&apos;s quota: <strong>{Math.round(consumed)}</strong> / {quota}s used
          {activeCount > 0 ? <> · {activeCount} active job{activeCount === 1 ? '' : 's'}</> : null}
          {configError ? <> · limits unavailable</> : null}
        </p>
      ) : null}

      <section className={styles.jobs} aria-labelledby="video-jobs-title">
        <header>
          <div>
            <span className={styles.kicker}>Production queue</span>
            <h2 id="video-jobs-title">Reels</h2>
          </div>
          <span>{jobs?.length ?? '—'}</span>
        </header>
        {jobs === null && !jobsError ? (
          <div className={styles.skeleton} aria-hidden="true"><span /><span /></div>
        ) : jobs?.length ? (
          <ul>
            {jobs.map((job, index) => {
              const media = displayMedia.find((item) => item.id === job.media_id);
              const active = ACTIVE_STATUSES.includes(job.status);
              return (
                <li key={job.id || `${job.created_at}:${index}`} data-status={job.status}>
                  <div className={styles.jobIcon}>
                    {job.kind === 'image' ? <ImagePlus aria-hidden="true" /> : <Type aria-hidden="true" />}
                  </div>
                  <div className={styles.jobMain}>
                    <strong>{job.source?.property?.address || `Reel ${index + 1}`}</strong>
                    <small>{job.kind} · {job.size} · {job.source?.voiceover ? 'voiceover' : 'no voiceover'} · {job.source?.captions === false ? 'no captions' : 'captions'}</small>
                    {job.status === 'failed' && job.error ? <p className={styles.jobError}>{job.error}</p> : null}
                  </div>
                  {job.status === 'ready' && media?.display_url ? (
                    <video
                      className={styles.thumb}
                      src={media.display_url}
                      preload="metadata"
                      muted
                      playsInline
                      aria-label={`Reel preview for ${job.source?.property?.address || 'property'}`}
                    />
                  ) : null}
                  <span className={styles.statusPill} data-status={job.status}>
                    {active ? <Loader2 className={styles.spin} aria-hidden="true" /> : null}
                    {statusLabel(job)}
                  </span>
                  <div className={styles.jobActions}>
                    {job.status === 'ready' && media?.display_url ? (
                      <a
                        href={media.display_url}
                        download={`reel-${job.id.slice(0, 8)}.mp4`}
                        className={styles.link}
                        aria-label="Download reel"
                      >
                        <Play aria-hidden="true" />
                      </a>
                    ) : null}
                    {ACTIVE_STATUSES.includes(job.status) ? (
                      <button type="button" onClick={() => cancelJob(job)} aria-label="Cancel job">
                        <X aria-hidden="true" />
                      </button>
                    ) : null}
                    <button type="button" onClick={() => deleteJob(job)} aria-label="Delete job">
                      <Trash2 aria-hidden="true" />
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        ) : !jobsError ? (
          <div className={styles.empty} role="status">
            <Clapperboard aria-hidden="true" />
            <div>
              <strong>No reels yet</strong>
              <p>Create your first reel from a property dossier or straight from photos.</p>
            </div>
            <button type="button" onClick={() => setCreating(true)}>Create first reel</button>
          </div>
        ) : null}
      </section>

      {creating ? (
        <section className={styles.builder} aria-labelledby="video-builder-title">
          <header className={styles.builderHead}>
            <div>
              <span className={styles.kicker}>New production</span>
              <h2 id="video-builder-title">Reel setup</h2>
            </div>
            <button type="button" className={styles.close} onClick={resetForm} aria-label="Close reel setup">
              <X aria-hidden="true" />
            </button>
          </header>

          <div className={styles.builderBody}>
            <div className={styles.form}>
              <fieldset>
                <legend>Property</legend>
                <label><span>Address</span><input value={address} onChange={(event) => setAddress(event.target.value)} placeholder="123 Main St, Wilmington, DE" /></label>
                <div className={styles.splitRow}>
                  <label><span>Lead ID</span><input value={leadId} onChange={(event) => setLeadId(event.target.value)} placeholder="Optional — ties the reel to a lead" inputMode="text" /></label>
                  <label><span>Listing ID</span><input value={listingId} onChange={(event) => setListingId(event.target.value)} placeholder="Optional — ties the reel to a listing" inputMode="text" /></label>
                </div>
              </fieldset>

              <fieldset>
                <legend>Format</legend>
                <div className={styles.sizeChoice} role="group" aria-label="Video format">
                  <button type="button" data-selected={size === '720x1280'} onClick={() => setSize('720x1280')}>
                    <span className={styles.portraitMark} aria-hidden="true" />
                    <strong>Portrait 9:16</strong>
                    <small>Instagram · TikTok · Shorts</small>
                  </button>
                  <button type="button" data-selected={size === '1280x720'} onClick={() => setSize('1280x720')}>
                    <span className={styles.landscapeMark} aria-hidden="true" />
                    <strong>Landscape 16:9</strong>
                    <small>YouTube · listing pages</small>
                  </button>
                </div>
              </fieldset>

              <fieldset>
                <legend>Source</legend>
                <div className={styles.kindChoice} role="group" aria-label="Video source">
                  <label className={styles.kindOption}>
                    <input type="radio" name="kind" checked={kind === 'text'} onChange={() => setKind('text')} />
                    <span><strong>Script to video</strong><small>Single clip from the narration</small></span>
                  </label>
                  <label className={styles.kindOption}>
                    <input type="radio" name="kind" checked={kind === 'image'} onChange={() => setKind('image')} />
                    <span><strong>Photos to video</strong><small>Up to {config?.max_images || 4} photos stitched into one reel</small></span>
                  </label>
                </div>
                {kind === 'image' ? (
                  <div className={styles.photoStrip} aria-label="Property photos">
                    {photos === null ? (
                      <p className={styles.photoHint}>
                        Enter a lead or listing ID to load its photos.
                      </p>
                    ) : displayPhotos.length ? (
                      <ul>
                        {displayPhotos.map((photo) => {
                          const selected = selectedPhotos.includes(photo.id);
                          return (
                            <li key={photo.id}>
                              <button
                                type="button"
                                data-selected={selected}
                                onClick={() => togglePhoto(photo.id)}
                                aria-pressed={selected}
                                aria-label={`Select photo ${photo.sort_order + 1}`}
                              >
                                {photo.display_url ? <img src={photo.display_url} alt="" loading="lazy" /> : <ImagePlus aria-hidden="true" />}
                                {selected ? <Check className={styles.photoCheck} aria-hidden="true" /> : null}
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    ) : (
                      <p className={styles.photoHint}>
                        No photos found for this property yet. Upload them from the CRM first.
                      </p>
                    )}
                    {kind === 'image' && (
                      <small className={styles.photoCount}>
                        {selectedPhotos.length}/{config?.max_images || 4} selected
                      </small>
                    )}
                  </div>
                ) : null}
              </fieldset>

              <fieldset>
                <legend>Narration</legend>
                <label className={styles.checkRow}>
                  <input type="checkbox" checked={voiceover} onChange={(event) => setVoiceover(event.target.checked)} />
                  <span>
                    <strong>Native voiceover</strong>
                    <small>Sora generates synchronized spoken audio — no separate TTS.</small>
                  </span>
                </label>
                <label className={styles.checkRow}>
                  <input type="checkbox" checked={captions} onChange={(event) => setCaptions(event.target.checked)} />
                  <span>
                    <strong>Burned-in captions</strong>
                    <small>The script is drawn onto the frames. Social feeds autoplay muted — without this the reel says nothing.</small>
                  </span>
                </label>
                <label>
                  <span>Reel script</span>
                  <textarea
                    value={script}
                    onChange={(event) => setScript(event.target.value)}
                    rows={5}
                    placeholder="Leave empty to auto-draft from the address…"
                  />
                </label>
                <button type="button" className={styles.secondary} onClick={draftScript} disabled={draftingScript}>
                  <Wand2 aria-hidden="true" /> {draftingScript ? 'Drafting…' : 'Draft script'}
                </button>
              </fieldset>

              {submitError ? <p className={styles.saveError} role="alert">{submitError}</p> : null}
              {created ? (
                <p className={styles.created} role="status">
                  <Check aria-hidden="true" /> Job queued — the reel will appear in the queue above.
                </p>
              ) : null}

              <footer className={styles.builderActions}>
                <button type="button" className={styles.secondary} onClick={resetForm} disabled={submitting}>Cancel</button>
                <button
                  type="button"
                  className={styles.primary}
                  disabled={!size || submitting || (kind === 'image' && selectedPhotos.length === 0) || (!leadId.trim() && !listingId.trim())}
                  onClick={submitJob}
                >
                  {submitting ? 'Queuing…' : 'Generate reel'} <Clapperboard aria-hidden="true" />
                </button>
              </footer>
            </div>
          </div>
        </section>
      ) : null}
    </section>
  );
}
