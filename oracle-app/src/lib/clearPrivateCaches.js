// What the browser keeps from one signed-in brokerage must not outlive the
// session. The service worker stores 3D splats and legal payloads in IndexedDB
// (served online only after the server re-checks access, L16-05, and offline
// from the copy), so on a shared device the previous brokerage's media must
// not stay on disk (security review WEB-10). Called on every sign-out and
// session expiry; best-effort by design — a failure here never blocks sign-out.
const SW_DB = 'oracle-predictive-cache';
// Capture Studio stages un-uploaded capture originals here (captureStore.js,
// L12-03): a walkthrough of a client's home is theirs, not the next user's.
// Signing out discards what was not uploaded; the studio says so.
const CAPTURE_DB = 'neoh-capture';
const PRIVATE_DBS = [SW_DB, CAPTURE_DB];

// localStorage keys that identify the person or hold their working data. The
// sign-in email and tenant id outlived sign-out, so a shared computer showed
// the next user who had been here (privacy data map §5). Theme and tour
// progress are device preferences and stay.
const PRIVATE_LOCAL_KEYS = ['oracle_user_id', 'oracle_tenant_id', 'oracle_comms_templates_v1'];

// WEB-001: 'blocked' used to count as deleted. A blocked delete stays queued
// until the open handles close (they now close themselves on versionchange),
// so wait for its real outcome, bounded so sign-out is never held up long.
const BLOCKED_WAIT_MS = 3000;

function deleteDatabase(name, blockedWaitMs) {
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    const finish = (outcome) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolve(outcome);
    };
    const req = indexedDB.deleteDatabase(name);
    req.onsuccess = () => finish('deleted');
    req.onerror = () => finish('error');
    req.onblocked = () => { timer = setTimeout(() => finish('blocked'), blockedWaitMs); };
  });
}

/** Returns { blocked: [...] }: databases whose deletion was still blocked. */
export async function clearPrivateCaches({ blockedWaitMs = BLOCKED_WAIT_MS } = {}) {
  const blocked = [];
  try {
    for (const key of PRIVATE_LOCAL_KEYS) window.localStorage.removeItem(key);
  } catch { /* storage blocked */ }
  try {
    navigator.serviceWorker?.controller?.postMessage({ type: 'CLEAR_PRIVATE_CACHE' });
  } catch { /* no service worker */ }
  for (const name of PRIVATE_DBS) {
    try {
      if ((await deleteDatabase(name, blockedWaitMs)) === 'blocked') blocked.push(name);
    } catch { /* no IndexedDB */ }
  }
  try {
    if (typeof caches !== 'undefined') {
      const names = await caches.keys();
      await Promise.all(names.map((name) => caches.delete(name)));
    }
  } catch { /* no Cache API */ }
  return { blocked };
}
