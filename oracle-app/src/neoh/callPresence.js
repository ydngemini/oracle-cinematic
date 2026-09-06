/**
 * callPresence — the one living fact the browser knows before the server.
 *
 * The agent's softphone runs in this tab. When it is ringing or connected to
 * someone, that person's object should be in call mode NOW, not after the next
 * fetch. The dialer publishes here; any living object subscribes by contact
 * and client id. Nothing here is persisted or sent anywhere — it is presence,
 * and it evaporates with the tab.
 */
import { useSyncExternalStore } from 'react';

const EMPTY = Object.freeze({});
let presence = EMPTY;
const listeners = new Set();

function emit() { for (const l of listeners) l(); }

/**
 * @param {{contactId?: string, clientId?: string, state: string,
 *          startedAt?: string, endedAt?: string} | null} next
 */
export function setCallPresence(next) {
  presence = next ? Object.freeze({ ...next }) : EMPTY;
  emit();
}

export function getCallPresence() { return presence; }

function subscribe(l) { listeners.add(l); return () => listeners.delete(l); }

/** Presence for one person, matched by client id OR contact id, else null. */
export function useCallPresence({ clientId, contactId } = {}) {
  const p = useSyncExternalStore(subscribe, getCallPresence, getCallPresence);
  if (p === EMPTY) return null;
  if (clientId && p.clientId === clientId) return p;
  if (contactId && p.contactId === contactId) return p;
  return null;
}

/** Test seam. */
export function resetCallPresence() { presence = EMPTY; emit(); }
