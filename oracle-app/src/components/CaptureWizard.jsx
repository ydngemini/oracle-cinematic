import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import MediaUploader from './MediaUploader';
import styles from './CaptureWizard.module.css';

/**
 * CaptureWizard — guided capture → Gaussian-splat reconstruction flow.
 *
 * Three steps on a filament rail:
 *   1. GUIDE   — how to shoot a house so it reconstructs well.
 *   2. CAPTURE — embeds the shared <MediaUploader/> (upload as-is) + a live
 *                photo count and a readiness hint.
 *   3. BUILD   — POST /api/crm/reconstruction-jobs → poll its status every ~3s
 *                → on success, resolve the property-tour splat and hand it up via
 *                onComplete(splatUrl) so the existing "Step inside" button lights.
 *
 * Honest degradation: a 503 from the enqueue means no reconstruction provider is
 * configured on this workspace — we say so plainly instead of crashing. A failed
 * job surfaces its error. Polling timers are torn down on unmount / step change.
 *
 * Props: { leadId, listingId, token, onClose, onComplete }
 */

const MIN_PHOTOS = 20; // ~20+ photos for a usable reconstruction
const POLL_MS = 3000;

const DISCLOSURE =
  'AI-generated 3D reconstruction from photos — geometry may be incomplete or inaccurate. Not a measured survey or a substitute for an in-person showing.';

const STEPS = ['Guide', 'Capture', 'Build'];

const TIPS = [
  'Keep 70–80% overlap between consecutive shots — every photo should share most of its frame with the last.',
  'Walk slowly and cover every corner of the room from several heights (low, eye-level, high).',
  'Aim for even, diffuse lighting — open blinds, turn on every lamp, avoid harsh shadows and blown-out windows.',
  'Avoid shooting mirrors, glass, and windows head-on — reflections and transparency confuse reconstruction.',
  'Blank, featureless walls reconstruct poorly — include doorframes, furniture, and trim for the solver to lock onto.',
  'Capture one room at a time, then move through doorways so rooms stitch together.',
];

const GLYPHS = {
  close: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
      <path d="M6 6l12 12M18 6 6 18" />
    </svg>
  ),
  check: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12.5 10 17.5 19 7" />
    </svg>
  ),
  walk: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20h13V9.5" />
      <circle cx="12" cy="13.5" r="2" />
    </svg>
  ),
  spark: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4" />
      <path d="m6.5 6.5 2.5 2.5M15 15l2.5 2.5M17.5 6.5 15 9M9 15l-2.5 2.5" />
    </svg>
  ),
};

export default function CaptureWizard({ leadId, listingId, token, onClose, onComplete }) {
  const [step, setStep] = useState(0); // 0 guide · 1 capture · 2 build
  const [roomName, setRoomName] = useState('');
  const [photoCount, setPhotoCount] = useState(0);

  // Build lifecycle: idle → queuing → running → succeeded | failed | unavailable
  const [gen, setGen] = useState('idle');
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState('queued'); // queued | running (label only)
  const [progress, setProgress] = useState(0);
  const [errMsg, setErrMsg] = useState('');
  const [splatUrl, setSplatUrl] = useState(null);

  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const pollRef = useRef(null);

  const enough = photoCount >= MIN_PHOTOS;

  const ownerQS = useMemo(() => {
    const p = new URLSearchParams();
    if (leadId != null) p.set('lead_id', String(leadId));
    if (listingId != null) p.set('listing_id', String(listingId));
    return p.toString();
  }, [leadId, listingId]);

  // ── Focus management — focus close on open, restore on unmount ──────────────
  useEffect(() => {
    const previouslyFocused = document.activeElement;
    closeRef.current?.focus();
    return () => previouslyFocused?.focus?.();
  }, []);

  // ── Escape to close + Tab trap within the dialog ───────────────────────────
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') { onClose?.(); return; }
      if (e.key !== 'Tab') return;
      const focusable = [...(dialogRef.current?.querySelectorAll(
        'button:not([disabled]), a[href], input, textarea, [tabindex]:not([tabindex="-1"])'
      ) ?? [])].filter((el) => el.offsetParent !== null);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // ── Poll the reconstruction job while it runs ──────────────────────────────
  // setState lives only inside async callbacks / setTimeout — never synchronously
  // in the effect body — so react-hooks/set-state-in-effect stays satisfied.
  useEffect(() => {
    if (gen !== 'running' || !jobId) return undefined;
    let cancelled = false;

    const tick = () => {
      crmGet(`/api/crm/reconstruction-jobs/${jobId}`).then(
        (d) => {
          if (cancelled) return;
          setProgress(Math.max(0, Math.min(100, Math.round(Number(d?.progress) || 0))));
          if (d?.status === 'succeeded') {
            setJobStatus('succeeded');
            // The media now exists — resolve the property's splat so we can offer
            // an immediate walk and light the parent's "Step inside" button.
            crmGet(`/api/crm/property-tour?${ownerQS}`).then(
              (t) => { if (!cancelled) { setSplatUrl(t?.splat_url || null); setGen('succeeded'); } },
              () => { if (!cancelled) setGen('succeeded'); },
            );
          } else if (d?.status === 'failed') {
            setErrMsg(d?.error || 'The reconstruction job failed.');
            setGen('failed');
          } else {
            setJobStatus(d?.status || 'running');
            pollRef.current = setTimeout(tick, POLL_MS);
          }
        },
        (err) => {
          if (cancelled) return;
          setErrMsg(err?.message || 'Lost contact with the reconstruction job.');
          setGen('failed');
        },
      );
    };

    tick();
    return () => {
      cancelled = true;
      if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }
    };
  }, [gen, jobId, ownerQS]);

  // ── Kick off a reconstruction (button handler — setState here is fine) ──────
  const startBuild = useCallback(async () => {
    setGen('queuing');
    setErrMsg('');
    setProgress(0);
    setSplatUrl(null);
    try {
      const d = await crmPost(`/api/crm/reconstruction-jobs?${ownerQS}`, {});
      setJobId(d?.job_id || null);
      setJobStatus(d?.status || 'queued');
      setGen('running'); // arms the polling effect
    } catch (err) {
      if (err?.status === 503) {
        setErrMsg(err?.message || '');
        setGen('unavailable');
      } else {
        setErrMsg(err?.message || 'Could not start the reconstruction.');
        setGen('failed');
      }
    }
  }, [ownerQS]);

  const resetBuild = useCallback(() => {
    if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }
    setGen('idle');
    setJobId(null);
    setErrMsg('');
    setProgress(0);
  }, []);

  const walkNow = useCallback(() => {
    onComplete?.(splatUrl);
  }, [onComplete, splatUrl]);

  const owner = { leadId, listingId };

  return (
    <div
      className={styles.overlay}
      role="presentation"
      onClick={(e) => { if (e.target === e.currentTarget) onClose?.(); }}
    >
      <div
        ref={dialogRef}
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-label="Create a 3D walkthrough"
      >
        <header className={styles.head}>
          <span className={styles.kicker}>3D Walkthrough</span>
          <button ref={closeRef} type="button" className={styles.closeBtn} onClick={() => onClose?.()} aria-label="Close">
            {GLYPHS.close}
          </button>
        </header>

        {/* Filament progress rail */}
        <nav className={styles.rail} aria-label={`Step ${step + 1} of ${STEPS.length}`}>
          {STEPS.map((label, i) => (
            <Fragment key={label}>
              {i > 0 && (
                <span className={`${styles.railLine}${i <= step ? ` ${styles.railLineLit}` : ''}`} aria-hidden="true" />
              )}
              <span
                className={`${styles.railNode}${i === step ? ` ${styles.railNodeActive}` : ''}${i < step ? ` ${styles.railNodeDone}` : ''}`}
                aria-current={i === step ? 'step' : undefined}
              >
                <span className={styles.railDot}>{i < step ? GLYPHS.check : i + 1}</span>
                <span className={styles.railLabel}>{label}</span>
              </span>
            </Fragment>
          ))}
        </nav>

        <div className={styles.body}>
          {/* ── Step 1 · GUIDE ─────────────────────────────────────────────── */}
          {step === 0 && (
            <section className={styles.stage} aria-label="How to capture">
              <h3 className={styles.stageTitle}>Capture the home for 3D</h3>
              <p className={styles.lede}>
                A good reconstruction is mostly about how you shoot. Follow these and the
                solver can rebuild a walkable space from your photos.
              </p>
              <ul className={styles.tips}>
                {TIPS.map((t) => (
                  <li key={t} className={styles.tip}>
                    <span className={styles.tipDot} aria-hidden="true" />
                    <span>{t}</span>
                  </li>
                ))}
              </ul>
              <label className={styles.field}>
                <span className={styles.microLabel}>Room name · Optional</span>
                <input
                  className={styles.input}
                  value={roomName}
                  onChange={(e) => setRoomName(e.target.value)}
                  placeholder="e.g. Primary bedroom"
                />
              </label>
              <div className={styles.actions}>
                <button type="button" className={styles.primaryBtn} onClick={() => setStep(1)}>
                  Start capturing
                </button>
              </div>
            </section>
          )}

          {/* ── Step 2 · CAPTURE ───────────────────────────────────────────── */}
          {step === 1 && (
            <section className={styles.stage} aria-label="Capture photos">
              <MediaUploader
                {...owner}
                token={token}
                title={roomName ? `Capture photos · ${roomName}` : 'Capture photos'}
                onChange={(media) => setPhotoCount(Array.isArray(media) ? media.length : 0)}
              />
              <div className={`${styles.readiness}${enough ? ` ${styles.readinessOk}` : ''}`}>
                <span className={styles.readinessCount}>
                  {photoCount} <span className={styles.readinessOf}>/ {MIN_PHOTOS}</span>
                </span>
                <span className={styles.readinessText}>
                  {enough
                    ? 'Enough photos to build a walkthrough — more coverage still helps.'
                    : 'Add ~20+ photos for a usable reconstruction.'}
                </span>
              </div>
              <div className={styles.actions}>
                <button type="button" className={styles.ghostBtn} onClick={() => setStep(0)}>
                  Back
                </button>
                <button type="button" className={styles.primaryBtn} onClick={() => setStep(2)}>
                  Next
                </button>
              </div>
            </section>
          )}

          {/* ── Step 3 · BUILD ─────────────────────────────────────────────── */}
          {step === 2 && (
            <section className={styles.stage} aria-label="Generate the walkthrough">
              {gen === 'succeeded' ? (
                <div className={styles.result} role="status">
                  <span className={styles.resultCheck} aria-hidden="true">{GLYPHS.check}</span>
                  <span className={styles.resultTitle}>Walkthrough ready</span>
                  <p className={styles.resultText}>
                    The 3D reconstruction succeeded. The “Step inside · walk the 3D space”
                    button is now available on this property.
                  </p>
                  <div className={styles.actions}>
                    {splatUrl ? (
                      <button type="button" className={styles.primaryBtn} onClick={walkNow}>
                        <span className={styles.btnGlyph} aria-hidden="true">{GLYPHS.walk}</span>
                        Walk it now
                      </button>
                    ) : (
                      <button type="button" className={styles.primaryBtn} onClick={() => onClose?.()}>
                        Done
                      </button>
                    )}
                  </div>
                </div>
              ) : gen === 'unavailable' ? (
                <div className={styles.notice} role="status">
                  <span className={styles.noticeTitle}>3D reconstruction isn’t enabled here yet</span>
                  <p className={styles.noticeText}>
                    3D reconstruction isn’t enabled on this workspace yet — an admin must
                    configure a reconstruction provider before walkthroughs can be built.
                  </p>
                  {errMsg && <p className={styles.noticeDetail}>{errMsg}</p>}
                  <div className={styles.actions}>
                    <button type="button" className={styles.ghostBtn} onClick={resetBuild}>
                      Try again
                    </button>
                    <button type="button" className={styles.primaryBtn} onClick={() => onClose?.()}>
                      Close
                    </button>
                  </div>
                </div>
              ) : gen === 'failed' ? (
                <div className={styles.errorBox} role="alert">
                  <span className={styles.errorTitle}>Reconstruction failed</span>
                  <p className={styles.errorText}>{errMsg || 'Something went wrong building the walkthrough.'}</p>
                  <div className={styles.actions}>
                    <button type="button" className={styles.primaryBtn} onClick={resetBuild}>
                      Try again
                    </button>
                  </div>
                </div>
              ) : gen === 'running' || gen === 'queuing' ? (
                <div className={styles.building} role="status" aria-live="polite">
                  <span className={styles.buildSpark} aria-hidden="true">{GLYPHS.spark}</span>
                  <span className={styles.buildTitle}>
                    {gen === 'queuing'
                      ? 'Starting the reconstruction…'
                      : jobStatus === 'queued'
                        ? 'Queued — waiting for a build slot…'
                        : 'Reconstructing the 3D space…'}
                  </span>
                  <span className={styles.progressTrack} aria-hidden="true">
                    <span className={styles.progressBar} style={{ width: `${progress}%` }} />
                  </span>
                  <span className={styles.progressPct}>{progress}%</span>
                  <p className={styles.buildHint}>
                    This can take a few minutes. You can keep this open — it updates on its own.
                  </p>
                </div>
              ) : (
                <div className={styles.generate}>
                  <h3 className={styles.stageTitle}>Build the 3D walkthrough</h3>
                  <p className={styles.lede}>
                    {enough
                      ? 'Your photos are ready. Generate the walkable Gaussian-splat reconstruction.'
                      : `Add at least ${MIN_PHOTOS} photos in the Capture step before building — you currently have ${photoCount}.`}
                  </p>
                  <div className={styles.actions}>
                    <button type="button" className={styles.ghostBtn} onClick={() => setStep(1)}>
                      Back
                    </button>
                    <button
                      type="button"
                      className={styles.primaryBtn}
                      onClick={startBuild}
                      disabled={!enough}
                    >
                      <span className={styles.btnGlyph} aria-hidden="true">{GLYPHS.spark}</span>
                      Generate 3D walkthrough
                    </button>
                  </div>
                </div>
              )}
            </section>
          )}
        </div>

        <p className={styles.disclosure}>{DISCLOSURE}</p>
      </div>
    </div>
  );
}
