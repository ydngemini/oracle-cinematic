import { precacheAndRoute, cleanupOutdatedCaches } from 'workbox-precaching';
import { registerRoute } from 'workbox-routing';
import { CacheFirst, NetworkFirst } from 'workbox-strategies';
import { ExpirationPlugin } from 'workbox-expiration';

// ─── Workbox Precache (static assets from build manifest) ────────────────────

precacheAndRoute(self.__WB_MANIFEST);
cleanupOutdatedCaches();

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

const SPLAT_BASE = '/public/splats';
const API_BASE = self.location.origin;
const MAX_CACHED_SPLATS = 5;

async function prefetchSplat(propertyId) {
  const existing = await idbGet(STORE_SPLATS, propertyId);
  if (existing?.blob) return;

  const url = `${API_BASE}${SPLAT_BASE}/${propertyId}.splat`;

  try {
    const resp = await fetch(url, { mode: 'cors', credentials: 'same-origin' });
    if (!resp.ok) return;

    const blob = await resp.blob();
    await idbPut(STORE_SPLATS, {
      propertyId,
      blob,
      size: blob.size,
      cachedAt: Date.now(),
    });

    await evictOldEntries(STORE_SPLATS, MAX_CACHED_SPLATS);
  } catch { /* network/IDB failure — prefetch is best-effort */ }
}

async function prefetchLegal(propertyId, legalPayload) {
  await idbPut(STORE_LEGAL, {
    propertyId,
    payload: legalPayload,
    cachedAt: Date.now(),
  });
}

async function evictOldEntries(storeName, maxEntries) {
  const db = await openDB();
  const tx = db.transaction(storeName, 'readwrite');
  const store = tx.objectStore(storeName);

  const allReq = store.getAll();
  allReq.onsuccess = () => {
    const entries = allReq.result;
    if (entries.length <= maxEntries) return;

    entries.sort((a, b) => (a.cachedAt || 0) - (b.cachedAt || 0));
    const toRemove = entries.slice(0, entries.length - maxEntries);
    for (const entry of toRemove) {
      store.delete(entry.propertyId);
    }
  };
}

// ─── Message Handler (from main thread) ──────────────────────────────────────

self.addEventListener('message', async (event) => {
  const { type, payload } = event.data || {};

  if (type === 'PREDICTIVE_CACHE') {
    const properties = payload?.properties || [];

    const tasks = properties.map(async (prop) => {
      const pid = prop.propertyId || prop.property_id;
      if (!pid) return;

      await prefetchSplat(pid);

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

registerRoute(
  ({ url }) => url.pathname.startsWith(SPLAT_BASE) && url.pathname.endsWith('.splat'),
  async ({ request }) => {
    const filename = request.url.split('/').pop();
    const propertyId = filename.replace('.splat', '');

    const cached = await idbGet(STORE_SPLATS, propertyId);
    if (cached?.blob) {
      return new Response(cached.blob, {
        status: 200,
        headers: {
          'Content-Type': 'application/octet-stream',
          'Content-Length': cached.blob.size.toString(),
          'X-Oracle-Cache': 'predictive-hit',
        },
      });
    }

    const response = await fetch(request);

    if (response.ok) {
      const clone = response.clone();
      const blob = await clone.blob();
      idbPut(STORE_SPLATS, {
        propertyId,
        blob,
        size: blob.size,
        cachedAt: Date.now(),
      }).catch(() => {});
    }

    return response;
  }
);

// ─── Cache static assets aggressively ────────────────────────────────────────

registerRoute(
  ({ request }) =>
    request.destination === 'script' ||
    request.destination === 'style' ||
    request.destination === 'font',
  new CacheFirst({
    cacheName: 'oracle-static-v1',
    plugins: [
      new ExpirationPlugin({ maxEntries: 100, maxAgeSeconds: 30 * 24 * 60 * 60 }),
    ],
  })
);

registerRoute(
  ({ request }) => request.destination === 'document',
  new NetworkFirst({
    cacheName: 'oracle-pages-v1',
    plugins: [
      new ExpirationPlugin({ maxEntries: 10, maxAgeSeconds: 7 * 24 * 60 * 60 }),
    ],
  })
);
