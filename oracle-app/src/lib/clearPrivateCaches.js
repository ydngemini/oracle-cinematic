// What the browser keeps from one signed-in brokerage must not outlive the
// session. The service worker stores 3D splats and legal payloads in IndexedDB
// and answers /api/media/{id} from it with no auth check, so on a shared
// device the next person to sign in could read the previous brokerage's
// cached media (security review WEB-10). Called on every sign-out and session
// expiry; best-effort by design — a failure here must never block sign-out.
const SW_DB = 'oracle-predictive-cache';

export async function clearPrivateCaches() {
  try {
    navigator.serviceWorker?.controller?.postMessage({ type: 'CLEAR_PRIVATE_CACHE' });
  } catch { /* no service worker */ }
  try {
    await new Promise((resolve) => {
      const req = indexedDB.deleteDatabase(SW_DB);
      req.onsuccess = req.onerror = req.onblocked = () => resolve();
    });
  } catch { /* no IndexedDB */ }
  try {
    if (typeof caches !== 'undefined') {
      const names = await caches.keys();
      await Promise.all(names.map((name) => caches.delete(name)));
    }
  } catch { /* no Cache API */ }
}
