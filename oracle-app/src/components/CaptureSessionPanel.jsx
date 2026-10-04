import { useCallback, useEffect, useRef, useState } from 'react';
import { CAPTURE_STEPS } from '../lib/tour/captureGuide';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './CaptureSessionPanel.module.css';

/**
 * Neoh Space — turn the photos and video already uploaded for a property into
 * a 3D space someone can walk through.
 *
 * Written for a person holding a phone, not for someone who knows how 3D is
 * made: no tool names, no file formats, no percentages. The server reports a
 * build as a STAGE (space_status.py) because no step of the pipeline exposes a
 * real percentage, and an honest "Building your space" beats a fake "83%".
 *
 * Durable from the user's side too: on mount the panel asks the server for the
 * property's latest build and resumes watching it, so leaving the page never
 * loses track of a build that is still running.
 *
 * Cost guard, visible: a second tap returns the same build (idempotency key +
 * the server's one-active-build rule); replacing a published space asks first;
 * a failed conversion retries without rebuilding.
 *
 * Props: { leadId, listingId, photoCount, videoCount, onComplete }
 */

const POLL_MS = 4000;

// Below this the build is refused before any processing starts. The server
// enforces its own gate; this only stops someone starting a build that will be
// refused.
const MIN_USEFUL_PHOTOS = 8;

function newKey() {
  try {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  } catch { /* fall through */ }
  return `k-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export default function CaptureSessionPanel({
  leadId, listingId, photoCount = 0, videoCount = 0, onComplete,
}) {
  // idle | starting | watching | confirm | done | failed | unavailable
  const [phase, setPhase] = useState('idle');
  const [job, setJob] = useState(null);
  const [published, setPublished] = useState(null);
  const [message, setMessage] = useState('');
  const [guideOpen, setGuideOpen] = useState(false);
  const pollRef = useRef(null);
  const keyRef = useRef(null);

  const ownerQS = leadId
    ? `lead_id=${encodeURIComponent(leadId)}`
    : listingId
      ? `listing_id=${encodeURIComponent(listingId)}`
      : '';

  // Resume: what does this property already have, and is a build running?
  useEffect(() => {
    if (!ownerQS) return undefined;
    let cancelled = false;
    crmGet(`/api/crm/space?${ownerQS}`).then(
      (summary) => {
        if (cancelled) return;
        setPublished(summary?.published || null);
        const latest = summary?.latest_build || null;
        if (latest) {
          setJob(latest);
          if (latest.active) setPhase('watching');
          else if (latest.state === 'failed' || latest.state === 'needs_attention') setPhase('failed');
        }
      },
      () => { /* no summary is not an error worth showing; start stays available */ },
    );
    return () => { cancelled = true; };
  }, [ownerQS]);

  // Poll while a build runs. Every setState is inside an async callback.
  useEffect(() => {
    if (phase !== 'watching' || !job?.job_id) return undefined;
    let cancelled = false;
    const tick = () => {
      crmGet(`/api/crm/reconstruction-jobs/${job.job_id}`).then(
        (next) => {
          if (cancelled) return;
          setJob(next);
          if (next?.state === 'ready') {
            setPhase('done');
            setPublished((p) => p || { media_id: next.media_id });
            onComplete?.();
          } else if (next?.terminal) {
            setPhase('failed');
          } else {
            pollRef.current = setTimeout(tick, POLL_MS);
          }
        },
        () => {
          if (cancelled) return;
          // A dropped poll is not a failed build. Keep watching, slower.
          pollRef.current = setTimeout(tick, POLL_MS * 3);
        },
      );
    };
    tick();
    return () => {
      cancelled = true;
      if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }
    };
  }, [phase, job?.job_id, onComplete]);

  const start = useCallback(async ({ confirmed = false } = {}) => {
    if (!ownerQS) return;
    setPhase('starting');
    setMessage('');
    // One key per intent: a double tap or a retried request reuses it, so the
    // server returns the same build instead of starting a second one.
    if (!keyRef.current) keyRef.current = newKey();
    const qs = `${ownerQS}&idempotency_key=${encodeURIComponent(keyRef.current)}${confirmed ? '&confirm_rebuild=true' : ''}`;
    try {
      const created = await crmPost(`/api/crm/reconstruction-jobs?${qs}`, {});
      setJob(created);
      setPhase(created?.terminal ? (created.state === 'ready' ? 'done' : 'failed') : 'watching');
      keyRef.current = null;
    } catch (error) {
      if (error?.status === 409) {
        setMessage(error?.message || 'This property already has a 3D space.');
        setPhase('confirm');
        return;
      }
      keyRef.current = null;
      setMessage(error?.message || 'Could not start building the space.');
      setPhase(error?.status === 503 ? 'unavailable' : 'failed');
    }
  }, [ownerQS]);

  const retry = useCallback(async () => {
    if (job?.retry_kind === 'conversion' && job?.job_id) {
      setPhase('starting');
      try {
        const next = await crmPost(`/api/crm/reconstruction-jobs/${job.job_id}/retry`, {});
        setJob(next);
        setPhase('watching');
      } catch (error) {
        setMessage(error?.message || 'Could not retry.');
        setPhase('failed');
      }
      return;
    }
    start({ confirmed: Boolean(published) });
  }, [job, published, start]);

  const total = photoCount + videoCount;
  const enough = photoCount >= MIN_USEFUL_PHOTOS || videoCount > 0;
  const steps = Array.isArray(job?.steps) ? job.steps : [];
  const watching = phase === 'watching' || phase === 'starting';

  return (
    <section className={styles.panel} aria-labelledby="capture-session-title" data-space-phase={phase}>
      <header className={styles.head}>
        <div>
          <h3 id="capture-session-title">Neoh Space</h3>
          <p className={styles.sub}>
            Build a 3D space from the photos and video uploaded for this property.
            It usually takes 20–60 minutes, and you can leave this page.
          </p>
        </div>
        <button
          type="button"
          className={styles.link}
          aria-expanded={guideOpen}
          aria-controls="capture-guide"
          onClick={() => setGuideOpen((open) => !open)}
        >
          {guideOpen ? 'Hide capture guide' : 'How to capture'}
        </button>
      </header>

      {guideOpen && (
        <ol className={styles.tips} id="capture-guide">
          {CAPTURE_STEPS.map((step) => (
            <li key={step.title}><strong>{step.title}.</strong> {step.text}</li>
          ))}
        </ol>
      )}

      {/* What the build has to work with, before anything starts. */}
      {!watching && phase !== 'done' && (
        <p className={enough ? styles.ready : styles.notReady} role="status">
          {total === 0
            ? 'Nothing uploaded yet. Add interior photos or a walkthrough video above.'
            : enough
              ? `${photoCount} photo${photoCount === 1 ? '' : 's'}${videoCount ? ` and ${videoCount} video${videoCount === 1 ? '' : 's'}` : ''} ready to build from.`
              : `${photoCount} of at least ${MIN_USEFUL_PHOTOS} photos. More overlapping shots make a usable space far more likely.`}
        </p>
      )}

      {(watching || phase === 'done' || phase === 'failed') && job && (
        <div className={styles.progressWrap} aria-live="polite">
          <p className={styles.stageLabel}>
            <span>{job.label || 'Building your space'}</span>
          </p>
          {job.message ? <p className={styles.progressText}>{job.message}</p> : null}
          {steps.length > 0 && (
            <ol className={styles.steps} aria-label="Build steps">
              {steps.map((step) => (
                <li
                  key={step.stage}
                  className={styles[`step_${step.state}`] || ''}
                  aria-current={step.state === 'current' ? 'step' : undefined}
                >
                  {step.label}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}

      {phase === 'done' && job?.caveats?.length ? (
        <ul className={styles.caveats} aria-label="About this space">
          {job.caveats.map((c) => <li key={c}>{c}</li>)}
        </ul>
      ) : null}

      {phase === 'done' && (
        <p className={styles.ready} role="status">
          Your space is ready. Open it from the tour above.
          {job?.floorplan === 'unavailable' ? ' A floor plan could not be made from this capture.' : ''}
        </p>
      )}

      {phase === 'failed' && job?.guidance?.length ? (
        <div className={styles.guidance}>
          <p>How to get a better capture:</p>
          <ul>{job.guidance.map((g) => <li key={g}>{g}</li>)}</ul>
        </div>
      ) : null}

      {(phase === 'failed' || phase === 'unavailable' || phase === 'confirm') && message ? (
        <p className={phase === 'confirm' ? styles.notReady : styles.error} role={phase === 'confirm' ? 'status' : 'alert'}>
          {message}
        </p>
      ) : null}

      <div className={styles.actions}>
        {(phase === 'idle' || phase === 'done' || phase === 'unavailable') && (
          <button
            type="button"
            className={styles.primary}
            onClick={() => start()}
            disabled={!ownerQS || total === 0}
          >
            {published || phase === 'done' ? 'Rebuild space' : 'Build 3D space'}
          </button>
        )}
        {phase === 'confirm' && (
          <>
            <button type="button" className={styles.primary} onClick={() => start({ confirmed: true })}>
              Rebuild — keep the current space until it is ready
            </button>
            <button type="button" className={styles.secondary} onClick={() => { setPhase('idle'); setMessage(''); }}>
              Cancel
            </button>
          </>
        )}
        {phase === 'failed' && job?.can_retry && job?.retry_kind !== 'recapture' && (
          <button type="button" className={styles.primary} onClick={retry}>
            {job.retry_kind === 'conversion' ? 'Finish preparing the space' : 'Try again'}
          </button>
        )}
        {phase === 'failed' && (job?.retry_kind === 'recapture' || !job?.can_retry) && (
          <button type="button" className={styles.secondary} onClick={() => { setPhase('idle'); setMessage(''); }}>
            Build again after uploading more
          </button>
        )}
      </div>
    </section>
  );
}
