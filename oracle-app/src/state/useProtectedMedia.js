import { useEffect, useMemo, useRef, useState } from 'react';
import { crmGetBlob } from './useCrmApi';

const API_BASE = import.meta.env.VITE_API_BASE || '';

function mediaRequest(m) {
  if (!m) return { key: '', path: '', direct: '' };
  const key = String(m.id ?? m.url ?? '');
  const raw = m.url || (m.id != null ? `/api/media/${m.id}` : '');
  if (!raw) return { key, path: '', direct: '' };

  try {
    const base = API_BASE || window.location.origin;
    const resolved = new URL(raw, base);
    const apiOrigin = new URL(base).origin;
    if (resolved.origin === apiOrigin && resolved.pathname.startsWith('/api/media/')) {
      return { key, path: `${resolved.pathname}${resolved.search}`, direct: '' };
    }
    return { key, path: '', direct: resolved.href };
  } catch {
    return { key, path: '', direct: '' };
  }
}

/**
 * One bounded queue for every protected fetch on the page.
 *
 * Each hook instance used to fire its whole list at once. A property sheet
 * showing a 280-photo capture therefore opened 280 requests, exhausted the
 * browser's per-origin connection pool, and starved everything behind it — the
 * 3D tour sat on "Preparing 3D tour…" forever because its single request never
 * got a connection. A capture is 120-200 photos by design, so this was not an
 * edge case.
 *
 * Two rules fix it. Bound how many run at once, and let a caller say its
 * fetch matters more than a thumbnail: the tour is what the person asked for,
 * the grid is what happens to be on screen.
 */
const MAX_IN_FLIGHT = 6;   // the practical per-origin ceiling anyway
let inFlight = 0;
const waiting = [];        // [{ priority, run }], highest priority first

function pump() {
  while (inFlight < MAX_IN_FLIGHT && waiting.length > 0) {
    let best = 0;
    for (let i = 1; i < waiting.length; i += 1) {
      if (waiting[i].priority > waiting[best].priority) best = i;
    }
    const [task] = waiting.splice(best, 1);
    inFlight += 1;
    task.run().finally(() => { inFlight -= 1; pump(); });
  }
}

/** Run `job` when a slot frees. Higher `priority` goes first. */
function schedule(job, priority) {
  return new Promise((resolve) => {
    waiting.push({ priority, run: () => job().then(resolve, resolve) });
    pump();
  });
}

/**
 * Resolve tenant-protected media rows to short-lived browser object URLs.
 * Internal /api/media/* bytes are fetched with the NEOH JWT; external provider
 * URLs remain untouched. Every generated URL is revoked on replacement/unmount.
 * Uses AbortController to cancel in-flight requests on unmount or dependency change.
 */
export default function useProtectedMedia(items, { token, priority = 0 } = {}) {
  const requests = useMemo(
    () => (Array.isArray(items) ? items : []).map(mediaRequest),
    [items],
  );
  const requestKey = requests.map((r) => `${r.key}:${r.path}:${r.direct}`).join('|');
  const [resolved, setResolved] = useState({});
  const [errors, setErrors] = useState({});
  const abortRef = useRef(null);

  useEffect(() => {
    const abortController = new AbortController();
    abortRef.current = abortController;
    const { signal } = abortController;
    const objectUrls = [];

    Promise.all(requests.map(async ({ key, path, direct }) => {
      if (!key) return null;
      if (direct) return [key, direct];
      if (!path) return [key, ''];
      return schedule(async () => {
        // Checked inside the slot too: by the time one frees, the component
        // may be gone, and fetching then would waste a slot a live caller
        // wants.
        if (signal.aborted) return [key, ''];
        try {
          const blob = await crmGetBlob(path, { token, signal });
          if (signal.aborted) return [key, ''];
          const objectUrl = URL.createObjectURL(blob);
          objectUrls.push(objectUrl);
          return [key, objectUrl];
        } catch (err) {
          if (err.name === 'AbortError' || signal.aborted) return [key, ''];
          // Report it. A swallowed failure is indistinguishable from a fetch
          // that has not finished, which is how a broken 3D tour showed
          // "Preparing 3D tour…" forever instead of saying what went wrong.
          return [key, '', err];
        }
      }, priority);
    })).then((entries) => {
      if (signal.aborted) return;
      const done = entries.filter(Boolean);
      setResolved(Object.fromEntries(done.map(([key, url]) => [key, url])));
      setErrors(Object.fromEntries(
        done.filter(([, , err]) => err).map(([key, , err]) => [key, err]),
      ));
    });

    return () => {
      abortController.abort();
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [requests, requestKey, token, priority]);

  return (Array.isArray(items) ? items : []).map((item) => {
    const { key, direct } = mediaRequest(item);
    return { ...item, display_url: resolved[key] || direct || '', error: errors[key] || null };
  });
}
