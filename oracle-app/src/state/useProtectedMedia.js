import { useEffect, useMemo, useRef, useState } from 'react';
import { crmGetBlob } from './useCrmApi';

const API_BASE = import.meta.env.VITE_API_BASE
  || (import.meta.env.DEV ? 'http://localhost:8000' : '');

function mediaRequest(m) {
  if (!m) return { key: '', path: '', direct: '' };
  const key = String(m.id ?? m.url ?? '');
  const raw = m.url || (m.id != null ? `/api/media/${m.id}` : '');
  if (!raw) return { key, path: '', direct: '' };

  try {
    const resolved = new URL(raw, API_BASE);
    const apiOrigin = new URL(API_BASE).origin;
    if (resolved.origin === apiOrigin && resolved.pathname.startsWith('/api/media/')) {
      return { key, path: `${resolved.pathname}${resolved.search}`, direct: '' };
    }
    return { key, path: '', direct: resolved.href };
  } catch {
    return { key, path: '', direct: '' };
  }
}

/**
 * Resolve tenant-protected media rows to short-lived browser object URLs.
 * Internal /api/media/* bytes are fetched with the NEOH JWT; external provider
 * URLs remain untouched. Every generated URL is revoked on replacement/unmount.
 * Uses AbortController to cancel in-flight requests on unmount or dependency change.
 */
export default function useProtectedMedia(items, { token } = {}) {
  const requests = useMemo(
    () => (Array.isArray(items) ? items : []).map(mediaRequest),
    [items],
  );
  const requestKey = requests.map((r) => `${r.key}:${r.path}:${r.direct}`).join('|');
  const [resolved, setResolved] = useState({});
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
      try {
        const blob = await crmGetBlob(path, { token, signal });
        if (signal.aborted) return [key, ''];
        const objectUrl = URL.createObjectURL(blob);
        objectUrls.push(objectUrl);
        return [key, objectUrl];
      } catch (err) {
        if (err.name === 'AbortError' || signal.aborted) return [key, ''];
        return [key, ''];
      }
    })).then((entries) => {
      if (signal.aborted) return;
      setResolved(Object.fromEntries(entries.filter(Boolean)));
    });

    return () => {
      abortController.abort();
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [requests, requestKey, token]);

  return (Array.isArray(items) ? items : []).map((item) => {
    const { key, direct } = mediaRequest(item);
    return { ...item, display_url: resolved[key] || direct || '' };
  });
}
