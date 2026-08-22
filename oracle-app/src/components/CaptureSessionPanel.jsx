import { useCallback, useEffect, useRef, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './CaptureSessionPanel.module.css';

/**
 * Turn the photos already uploaded for a property into a walkable 3D capture.
 *
 * This is the enqueue + poll half of the old CaptureWizard, moved onto Property
 * View. The wizard was a self-contained three-step modal that owned its own
 * uploader — and had zero importers, so it was the only way to start a
 * reconstruction and no screen could reach it. Property View is the shipped
 * capture surface (photo + video + 360, surface tagging, client upload links,
 * review queue), so the job controls belong beside the photos they consume
 * rather than in a parallel flow that duplicates uploading.
 *
 * The guidance below is the wizard's genuinely valuable part: an amateur with a
 * phone produces an unreconstructable set without it, and the failure only
 * surfaces 20 minutes later at the end of a GPU job.
 *
 * Props: { leadId, listingId, photoCount, onComplete }
 */

const POLL_MS = 3000;

// Reconstruction needs overlapping views of textured surfaces. Each of these
// corresponds to a way the solver actually fails, not to generic photo advice.
const CAPTURE_TIPS = [
  'Keep 70–80% overlap between consecutive shots — every photo should share most of its frame with the last.',
  'Walk slowly and cover every corner from several heights (low, eye-level, high).',
  'Aim for even, diffuse light — open blinds, turn on every lamp, avoid harsh shadows and blown-out windows.',
  'Avoid shooting mirrors, glass and windows head-on; reflections and transparency confuse the solver.',
  'Blank walls reconstruct poorly — include doorframes, furniture and trim for the solver to lock onto.',
  'Capture one room at a time, then move through doorways so the rooms stitch together.',
];

// Below this the solver has too little to work with. The backend enforces its
// own minimum; this is here so the agent finds out before starting a job rather
// than after one fails.
const MIN_USEFUL_PHOTOS = 8;

export default function CaptureSessionPanel({ leadId, listingId, photoCount = 0, onComplete }) {
  // idle → queuing → running → succeeded | failed | unavailable
  const [phase, setPhase] = useState('idle');
  const [jobId, setJobId] = useState(null);
  const [progress, setProgress] = useState(0);
  const [message, setMessage] = useState('');
  const [tipsOpen, setTipsOpen] = useState(false);
  const pollRef = useRef(null);

  const ownerQS = leadId
    ? `lead_id=${encodeURIComponent(leadId)}`
    : listingId
      ? `listing_id=${encodeURIComponent(listingId)}`
      : '';

  // Poll while a job runs. Every setState is inside an async callback or a
  // timeout — never synchronously in the effect body.
  useEffect(() => {
    if (phase !== 'running' || !jobId) return undefined;
    let cancelled = false;

    const tick = () => {
      crmGet(`/api/crm/reconstruction-jobs/${jobId}`).then(
        (job) => {
          if (cancelled) return;
          setProgress(Math.max(0, Math.min(100, Math.round(Number(job?.progress) || 0))));
          if (job?.status === 'succeeded') {
            setPhase('succeeded');
            onComplete?.();
          } else if (job?.status === 'failed') {
            setMessage(job?.error || 'The reconstruction job failed.');
            setPhase('failed');
          } else {
            pollRef.current = setTimeout(tick, POLL_MS);
          }
        },
        (error) => {
          if (cancelled) return;
          setMessage(error?.message || 'Lost contact with the reconstruction job.');
          setPhase('failed');
        },
      );
    };

    tick();
    return () => {
      cancelled = true;
      if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }
    };
  }, [phase, jobId, onComplete]);

  const start = useCallback(async () => {
    if (!ownerQS) return;
    setPhase('queuing');
    setMessage('');
    setProgress(0);
    try {
      const job = await crmPost(`/api/crm/reconstruction-jobs?${ownerQS}`, {});
      setJobId(job?.job_id || null);
      setPhase('running');
    } catch (error) {
      if (error?.status === 503) {
        // The provider is not configured on this deployment. That is a
        // deployment fact, not a failure of this property's photos, and the
        // backend's message says which.
        setMessage(error?.message || 'Reconstruction is not available on this deployment.');
        setPhase('unavailable');
      } else {
        setMessage(error?.message || 'Could not start the reconstruction.');
        setPhase('failed');
      }
    }
  }, [ownerQS]);

  const reset = useCallback(() => {
    if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }
    setPhase('idle');
    setJobId(null);
    setProgress(0);
    setMessage('');
  }, []);

  const enoughPhotos = photoCount >= MIN_USEFUL_PHOTOS;
  const busy = phase === 'queuing' || phase === 'running';

  return (
    <section className={styles.panel} aria-labelledby="capture-session-title">
      <header className={styles.head}>
        <div>
          <h3 id="capture-session-title">Build a walkable 3D capture</h3>
          <p className={styles.sub}>
            Uses the photos already uploaded for this property. Takes 20–60 minutes.
          </p>
        </div>
        <button
          type="button"
          className={styles.link}
          aria-expanded={tipsOpen}
          onClick={() => setTipsOpen((open) => !open)}
        >
          {tipsOpen ? 'Hide shooting guide' : 'How to shoot it'}
        </button>
      </header>

      {tipsOpen && (
        <ul className={styles.tips}>
          {CAPTURE_TIPS.map((tip) => <li key={tip}>{tip}</li>)}
        </ul>
      )}

      {/* Say what the solver has to work with before a job is started, rather
          than after twenty minutes of GPU time. */}
      <p className={enoughPhotos ? styles.ready : styles.notReady} role="status">
        {photoCount === 0
          ? 'No photos uploaded yet. Add interior photos above to build a capture.'
          : enoughPhotos
            ? `${photoCount} photos uploaded — enough to attempt a reconstruction.`
            : `${photoCount} of at least ${MIN_USEFUL_PHOTOS} photos. More overlapping shots make a usable capture far more likely.`}
      </p>

      {phase === 'running' || phase === 'queuing' ? (
        <div className={styles.progressWrap}>
          <div
            className={styles.progressBar}
            role="progressbar"
            aria-valuenow={progress}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="Reconstruction progress"
          >
            <span className={styles.progressFill} style={{ width: `${progress}%` }} />
          </div>
          <p className={styles.progressText}>
            {phase === 'queuing' ? 'Queuing…' : `Building — ${progress}%`}
            {' · you can leave this page, the job keeps running.'}
          </p>
        </div>
      ) : null}

      {phase === 'succeeded' && (
        <p className={styles.ready} role="status">
          Capture complete. The property now offers a walkable 3D tour.
        </p>
      )}

      {(phase === 'failed' || phase === 'unavailable') && (
        <p className={styles.error} role="alert">
          {message}
        </p>
      )}

      <div className={styles.actions}>
        {phase === 'idle' || phase === 'succeeded' ? (
          <button
            type="button"
            className={styles.primary}
            onClick={start}
            disabled={!ownerQS || photoCount === 0}
          >
            {phase === 'succeeded' ? 'Rebuild capture' : 'Build 3D capture'}
          </button>
        ) : null}
        {(phase === 'failed' || phase === 'unavailable') && (
          <button type="button" className={styles.secondary} onClick={reset}>
            Try again
          </button>
        )}
        {busy && (
          <button type="button" className={styles.secondary} onClick={reset}>
            Stop watching
          </button>
        )}
      </div>
    </section>
  );
}
