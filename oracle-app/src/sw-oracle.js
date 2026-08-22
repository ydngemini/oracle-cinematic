import { ExpirationPlugin } from 'workbox-expiration';
import { registerRoute } from 'workbox-routing';
import { CacheFirst, NetworkFirst } from 'workbox-strategies';

// Bump the cache generation so activate removes pre-expiration v2 entries
// already retained by long-lived Azure SPA installations.
const STATIC_CACHE = 'neoh-static-v3';
const PAGE_CACHE = 'neoh-pages-v3';
const STATIC_CACHE_MAX_ENTRIES = 60;
const STATIC_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((key) => key.startsWith('neoh-') && ![STATIC_CACHE, PAGE_CACHE].includes(key))
          .map((key) => caches.delete(key)),
      ))
      .then(() => self.clients.claim()),
  );
});

// ─── IndexedDB Predictive Cache Engine ───────────────────────────────────────

const IDB_NAME = 'oracle-predictive-cache';
const IDB_VERSION = 1;
const STORE_SPLATS = 'splats';
const STORE_LEGAL = 'legal';
const STORE_META = 'meta';

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_NAME, IDB_VERSION);
    req.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains(STORE_SPLATS)) {
        db.createObjectStore(STORE_SPLATS, { keyPath: 'propertyId' });
      }
      if (!db.objectStoreNames.contains(STORE_LEGAL)) {
        db.createObjectStore(STORE_LEGAL, { keyPath: 'propertyId' });
      }
      if (!db.objectStoreNames.contains(STORE_META)) {
        db.createObjectStore(STORE_META, { keyPath: 'key' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbPut(storeName, data) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    tx.objectStore(storeName).put(data);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function idbGet(storeName, key) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readonly');
    const req = tx.objectStore(storeName).get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbDelete(storeName, key) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    tx.objectStore(storeName).delete(key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

// ─── Predictive Pre-Download Engine ──────────────────────────────────────────

// Reconstructions are served by the authenticated media route, keyed by the
// media row's id. They used to come from an unauthenticated `/public/splats`
// mount at a filename derived from the address — that mount is gone, and with
// it the ability to prefetch by address. `mediaId` here is the id the tour
// resolver hands back in `splat_url`, so a prefetch is only possible for a
// property the user has already been shown.
const API_BASE = self.location.origin;
// Splats are 10-60MB each, so a fixed count thrashes once a few listings are
// walked. Bound by total BYTES (primary) with a generous count as a secondary
// guard.
const MAX_CACHED_SPLATS = 30;
const MAX_CACHE_BYTES = 400 * 1024 * 1024; // ~400 MB of cached splats

async function prefetchSplat(mediaId) {
  // No media id means nothing has been captured for this property, or the
  // caller has not resolved its tour yet. Either way there is nothing to fetch.
  if (!mediaId) return;

  const existing = await idbGet(STORE_SPLATS, mediaId);
  if (existing?.blob) return;

  const url = `${API_BASE}/api/media/${mediaId}`;

  try {
    // same-origin credentials so the authenticated route accepts it; a 401/403
    // simply means this user may not read it, and `resp.ok` handles that.
    const resp = await fetch(url, { credentials: 'same-origin' });
    if (!resp.ok) return;

    const blob = await resp.blob();
    await idbPut(STORE_SPLATS, {
      // The store's keyPath is still named `propertyId` (renaming it would need
      // an IndexedDB version bump for no behavioural gain); the value is the
      // media id, which is what both the prefetch and the fetch interception
      // can actually see in a `/api/media/{id}` URL.
      propertyId: mediaId,
      blob,
      size: blob.size,
      cachedAt: Date.now(),
    });

    await evictOldEntries(STORE_SPLATS, MAX_CACHED_SPLATS, MAX_CACHE_BYTES);
  } catch { /* network/IDB failure — prefetch is best-effort */ }
}

async function prefetchLegal(propertyId, legalPayload) {
  await idbPut(STORE_LEGAL, {
    propertyId,
    payload: legalPayload,
    cachedAt: Date.now(),
  });
}

async function evictOldEntries(storeName, maxEntries, maxBytes = Infinity) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    const store = tx.objectStore(storeName);
    const allReq = store.getAll();
    allReq.onsuccess = () => {
      const entries = allReq.result;
      // Keep newest-first within BOTH the count and byte budgets; evict the rest.
      entries.sort((a, b) => (b.cachedAt || 0) - (a.cachedAt || 0));
      let count = 0;
      let bytes = 0;
      for (const entry of entries) {
        count += 1;
        bytes += entry.size || 0;
        if (count > maxEntries || bytes > maxBytes) {
          store.delete(entry.propertyId);
        }
      }
    };
    allReq.onerror = () => reject(allReq.error);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

// ─── Message Handler (from main thread) ──────────────────────────────────────

self.addEventListener('message', async (event) => {
  const { type, payload } = event.data || {};

  if (type === 'PREDICTIVE_CACHE') {
    const properties = payload?.properties || [];

    const tasks = properties.map(async (prop) => {
      const pid = prop.propertyId || prop.property_id;
      if (!pid) return;

      await prefetchSplat(prop.splatMediaId || prop.splat_media_id);

      if (prop.legalPayload) {
        await prefetchLegal(pid, prop.legalPayload);
      }
    });

    await Promise.allSettled(tasks);

    // Notify all clients that cache is warm
    const clients = await self.clients.matchAll();
    for (const client of clients) {
      client.postMessage({
        type: 'CACHE_WARM',
        propertyIds: properties.map((p) => p.propertyId || p.property_id).filter(Boolean),
      });
    }
  }

  if (type === 'CACHE_QUERY') {
    const propertyId = payload?.propertyId;
    if (!propertyId) return;

    const splatEntry = await idbGet(STORE_SPLATS, propertyId);
    const legalEntry = await idbGet(STORE_LEGAL, propertyId);

    event.source?.postMessage({
      type: 'CACHE_QUERY_RESULT',
      propertyId,
      hasSplat: !!splatEntry?.blob,
      splatSize: splatEntry?.blob?.size || 0,
      hasLegal: !!legalEntry?.payload,
    });
  }

  if (type === 'CACHE_RETRIEVE_SPLAT') {
    const propertyId = payload?.propertyId;
    if (!propertyId) return;

    const entry = await idbGet(STORE_SPLATS, propertyId);
    if (entry?.blob) {
      event.source?.postMessage({
        type: 'CACHE_SPLAT_DATA',
        propertyId,
        blob: entry.blob,
      });
    }
  }

  if (type === 'CACHE_RETRIEVE_LEGAL') {
    const propertyId = payload?.propertyId;
    if (!propertyId) return;

    const entry = await idbGet(STORE_LEGAL, propertyId);
    if (entry?.payload) {
      event.source?.postMessage({
        type: 'CACHE_LEGAL_DATA',
        propertyId,
        payload: entry.payload,
      });
    }
  }

  if (type === 'CACHE_PURGE') {
    const propertyId = payload?.propertyId;
    if (propertyId) {
      await idbDelete(STORE_SPLATS, propertyId);
      await idbDelete(STORE_LEGAL, propertyId);
    }
  }
});

// ─── Fetch Intercept: Serve splats from IndexedDB if cached ──────────────────

// Serve a prefetched reconstruction from IndexedDB. Deliberately does NOT
// store on the fetch path: this route now matches `/api/media/*`, which is
// every photo as well as every splat, and caching all of them here would fill
// the 400 MB splat budget with thumbnails. Only prefetchSplat() writes, and it
// only runs for ids the app has explicitly nominated.
async function serveSplat(request) {
  const mediaId = new URL(request.url).pathname.split('/').pop();
  const cached = await idbGet(STORE_SPLATS, mediaId);
  if (cached?.blob) {
    return new Response(cached.blob, {
      status: 200,
      headers: {
        'Content-Type': 'application/octet-stream',
        'Content-Length': cached.blob.size.toString(),
        'X-Neoh-Cache': 'predictive-hit',
      },
    });
  }
  return fetch(request);
}

registerRoute(
  ({ url }) => (
    url.origin === self.location.origin
    && url.pathname.startsWith('/api/media/')
  ),
  ({ request }) => serveSplat(request),
);

registerRoute(
  ({ request, url }) => (
    url.origin === self.location.origin
    && ['script', 'style', 'font'].includes(request.destination)
  ),
  new CacheFirst({
    cacheName: STATIC_CACHE,
    plugins: [
      new ExpirationPlugin({
        maxEntries: STATIC_CACHE_MAX_ENTRIES,
        maxAgeSeconds: STATIC_CACHE_MAX_AGE_SECONDS,
      }),
    ],
  }),
);

registerRoute(
  ({ request, url }) => (
    url.origin === self.location.origin
    && request.destination === 'document'
  ),
  new NetworkFirst({ cacheName: PAGE_CACHE }),
);
