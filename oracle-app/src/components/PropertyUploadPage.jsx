import { useCallback, useEffect, useRef, useState } from 'react';
import styles from './PropertyUploadPage.module.css';

/**
 * PropertyUploadPage — the page a CLIENT lands on from an agent's capture link.
 *
 * Unauthenticated by design: the token in the URL is the whole capability. It
 * therefore shows the minimum needed to do the job — a display address, what is
 * accepted, and how many uploads remain. No comps, no valuation, no owner data,
 * because a capability URL gets forwarded and must not become a data leak.
 *
 * Mounted from App.jsx on /property-upload/:token, alongside /site-preview/.
 */

const API_BASE = import.meta.env.VITE_API_BASE
  || (import.meta.env.DEV ? 'http://localhost:8000' : '');

const SURFACES = [
  { id: 'exterior', label: 'Outside' },
  { id: 'interior', label: 'Inside' },
  { id: 'other', label: 'Something else' },
];

function tokenFromPath() {
  const parts = window.location.pathname.split('/').filter(Boolean);
  // /property-upload/:token
  return parts[1] ? decodeURIComponent(parts[1]) : '';
}

export default function PropertyUploadPage() {
  const [token] = useState(tokenFromPath);
  const [link, setLink] = useState(null);
  // A missing token is knowable at first render — derive it from initial state
  // rather than setting it from an effect (react-hooks/set-state-in-effect).
  const [state, setState] = useState(() => (tokenFromPath() ? 'checking' : 'invalid')); // checking|ready|invalid
  const [error, setError] = useState(() => (tokenFromPath() ? null : 'This link is missing its access code.'));
  const [surface, setSurface] = useState('exterior');
  const [queue, setQueue] = useState([]); // {name, status, error}
  const [busy, setBusy] = useState(false);
  const inputRef = useRef(null);

  useEffect(() => {
    if (!token) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/public/property-upload/${encodeURIComponent(token)}`);
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'This link is no longer valid.');
        const payload = await res.json();
        if (cancelled) return;
        setLink(payload);
        setState('ready');
      } catch (err) {
        if (cancelled) return;
        setState('invalid');
        setError(err?.message || 'This link is no longer valid.');
      }
    })();
    return () => { cancelled = true; };
  }, [token]);

  const upload = useCallback(async (event) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (!files.length) return;

    setBusy(true);
    // Sequential, not parallel: these are phone uploads on mobile data, and a
    // handful of concurrent 100 MB videos will stall or drop on cellular.
    for (const file of files) {
      const entry = { name: file.name, status: 'uploading', error: null };
      setQueue((q) => [...q, entry]);

      const form = new FormData();
      form.append('surface', surface);
      form.append('file', file);

      try {
        const res = await fetch(
          `${API_BASE}/api/public/property-upload/${encodeURIComponent(token)}`,
          { method: 'POST', body: form },
        );
        const payload = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(payload.detail || 'Upload failed.');
        setQueue((q) => q.map((i) => (i === entry ? { ...i, status: 'done' } : i)));
        if (typeof payload.remaining_uploads === 'number') {
          setLink((prev) => (prev ? { ...prev, remaining_uploads: payload.remaining_uploads } : prev));
        }
      } catch (err) {
        const message = err?.message || 'Upload failed.';
        setQueue((q) => q.map((i) => (i === entry ? { ...i, status: 'failed', error: message } : i)));
      }
    }
    setBusy(false);
  }, [surface, token]);

  if (state === 'checking') {
    return (
      <main className={styles.page}>
        <p className={styles.status} role="status">Checking your link…</p>
      </main>
    );
  }

  if (state === 'invalid') {
    return (
      <main className={styles.page}>
        <div className={styles.card}>
          <h1 className={styles.title}>Link unavailable</h1>
          <p className={styles.body} role="alert">{error}</p>
          <p className={styles.fine}>Ask your agent to send a new one.</p>
        </div>
      </main>
    );
  }

  const remaining = link?.remaining_uploads ?? 0;
  const exhausted = remaining <= 0;

  return (
    <main className={styles.page}>
      <div className={styles.card}>
        <p className={styles.kicker}>Photo &amp; video upload</p>
        <h1 className={styles.title}>{link?.address || 'Your property'}</h1>
        <p className={styles.body}>
          Send your agent photos or video of the property. {link?.notice}
        </p>

        <fieldset className={styles.surfaces}>
          <legend className={styles.legend}>What are you sending?</legend>
          {SURFACES.map(({ id, label }) => (
            <label key={id} className={surface === id ? styles.surfaceOn : styles.surface}>
              <input
                type="radio"
                name="surface"
                value={id}
                checked={surface === id}
                onChange={() => setSurface(id)}
              />
              {label}
            </label>
          ))}
        </fieldset>

        <button
          type="button"
          className={styles.cta}
          onClick={() => inputRef.current?.click()}
          disabled={busy || exhausted}
        >
          {busy ? 'Uploading…' : exhausted ? 'Upload limit reached' : 'Choose photos or video'}
        </button>
        <input
          ref={inputRef}
          type="file"
          multiple
          // capture= is intentionally omitted: it forces the camera and blocks
          // picking existing shots, which is what most people actually have.
          accept="image/*,video/mp4,video/quicktime,video/webm"
          className={styles.srOnly}
          onChange={upload}
        />

        <p className={styles.fine}>
          {remaining} upload{remaining === 1 ? '' : 's'} remaining · photos up to{' '}
          {link?.accepted?.max_photo_mb} MB, video up to {link?.accepted?.max_video_mb} MB
        </p>

        {queue.length > 0 && (
          <ul className={styles.queue}>
            {queue.map((item, i) => (
              <li key={`${item.name}-${i}`} data-status={item.status}>
                <span className={styles.queueName}>{item.name}</span>
                <span className={styles.queueStatus}>
                  {item.status === 'uploading' ? 'Sending…'
                    : item.status === 'done' ? 'Sent'
                      : item.error}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}
