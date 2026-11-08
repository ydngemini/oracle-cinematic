/**
 * On-device staging of capture originals until the server acknowledges them
 * (prompt 12, T12.05; ledger L12-03).
 *
 * A recording lives only in the tab until it is uploaded, so a reload, a
 * crash or a dropped connection used to lose it. Each recording or photo is
 * written here first, keyed by an id, with the property it belongs to; it is
 * removed only after the upload endpoint answered success for it. On the next
 * visit the studio lists what is still staged so it can be uploaded or
 * discarded explicitly.
 *
 * Storage is checked, not assumed: `stage` refuses (and says so) when the
 * browser reports less free quota than the file needs, or when no storage is
 * available at all (private mode) — the capture then stays in memory only and
 * the UI says it will be lost on reload.
 *
 * The adapter is IndexedDB in the browser; tests pass an in-memory one.
 */

const DB_NAME = 'neoh-capture';
const STORE = 'staged';

export function memoryAdapter() {
  const rows = new Map();
  return {
    async put(row) { rows.set(row.id, row); },
    async all() { return [...rows.values()]; },
    async remove(id) { rows.delete(id); },
  };
}

function request(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export function indexedDbAdapter(idb = globalThis.indexedDB) {
  if (!idb) return null;
  let opened = null;
  const db = () => {
    if (!opened) {
      const req = idb.open(DB_NAME, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: 'id' });
      opened = request(req);
    }
    return opened;
  };
  const tx = async (mode, fn) => {
    const store = (await db()).transaction(STORE, mode).objectStore(STORE);
    return request(fn(store));
  };
  return {
    put: (row) => tx('readwrite', (s) => s.put(row)),
    all: () => tx('readonly', (s) => s.getAll()),
    remove: (id) => tx('readwrite', (s) => s.delete(id)),
  };
}

export class StorageRefused extends Error {
  constructor(reason) {
    super(reason);
    this.reason = reason;
  }
}

export function newCaptureId() {
  try {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  } catch { /* fall through */ }
  return `c-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function createCaptureStore({ adapter = indexedDbAdapter(), estimate } = {}) {
  const quota = estimate ?? (() => globalThis.navigator?.storage?.estimate?.());

  return {
    available: Boolean(adapter),

    /** Stage one original. Resolves the row, or throws StorageRefused. */
    async stage(blob, { subject, name, kind }) {
      if (!adapter) throw new StorageRefused('no_storage');
      let room = null;
      try {
        const est = await quota();
        if (est && Number.isFinite(est.quota) && Number.isFinite(est.usage)) room = est.quota - est.usage;
      } catch { /* unknown quota: try the write, it fails loudly if full */ }
      if (room !== null && room < blob.size) throw new StorageRefused('quota');
      const row = { id: newCaptureId(), subject, name, kind, type: blob.type, size: blob.size, blob, stagedAt: new Date().toISOString() };
      try {
        await adapter.put(row);
      } catch {
        throw new StorageRefused('write_failed');
      }
      return row;
    },

    /** Everything still staged for this property, oldest first. */
    async pending(subject) {
      if (!adapter) return [];
      const rows = await adapter.all();
      return rows.filter((r) => r.subject === subject).sort((a, b) => a.stagedAt.localeCompare(b.stagedAt));
    },

    /** Drop a row: only after the server acknowledged it, or on an explicit discard. */
    async remove(id) {
      if (adapter) await adapter.remove(id);
    },
  };
}

export const STORAGE_REFUSED_MESSAGE = {
  no_storage: 'This browser cannot save captures on the device (private mode?). Upload before leaving this page — a reload would lose them.',
  quota: 'Not enough free space on this device to keep a copy. Upload now — a reload would lose this capture.',
  write_failed: 'Could not save a copy on this device. Upload now — a reload would lose this capture.',
};
