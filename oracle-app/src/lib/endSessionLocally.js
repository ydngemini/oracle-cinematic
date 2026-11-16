import { clearPrivateCaches } from './clearPrivateCaches.js';
import { safeRemove } from './safeStorage.js';

/**
 * Everything this browser does when a session ends (expiry or sign-out):
 * forget the role, clear the private caches, show the sign-in. Each step runs
 * even if an earlier one fails (WEB-002), so a storage error can never leave
 * a revoked session's CRM data on screen.
 */
export async function endSessionLocally(setAuthed) {
  try {
    safeRemove('sessionStorage', 'oracle_role');
    await clearPrivateCaches();
  } catch {
    /* the caches are best effort; the sign-in screen is not */
  } finally {
    setAuthed(false);
  }
}
