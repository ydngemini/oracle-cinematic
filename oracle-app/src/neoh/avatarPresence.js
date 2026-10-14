/**
 * avatarPresence — one resolved avatar state for every mascot on screen.
 *
 * The floating Neoh surface owns the facts (its microphone, its asks, the
 * thread) and resolves them once. The Home corner — flat SVG or 3D — reads
 * that same resolved value here instead of deriving its own, so the two
 * mascots can never show different states at once (F15.09).
 *
 * The surface publishes from a layout effect; useSyncExternalStore re-renders
 * the corner synchronously, so both paint the same state in the same frame.
 * With no publisher (the surface is not mounted, or Neoh is off for this
 * workspace) the corner rests at idle, which is what it always showed.
 *
 * Nothing is persisted or sent anywhere; it is presence, like callPresence.
 */
import { useSyncExternalStore } from 'react';

export const IDLE_AVATAR = Object.freeze({
  state: 'idle', actionType: 'generic', reason: null, audioLevel: 0, attentionLevel: 0,
});

let owners = [];
let current = IDLE_AVATAR;
const listeners = new Set();

function settle() {
  const next = owners.length ? owners[owners.length - 1].view : IDLE_AVATAR;
  if (next === current) return;
  current = next;
  for (const listener of listeners) listener();
}

/** The newest publisher wins; re-publishing keeps its place. */
export function publishAvatar(owner, view) {
  const index = owners.findIndex((entry) => entry.owner === owner);
  if (index >= 0 && owners[index].view === view) return;
  const entry = { owner, view: view || IDLE_AVATAR };
  owners = index >= 0
    ? owners.map((existing, i) => (i === index ? entry : existing))
    : [...owners, entry];
  settle();
}

export function withdrawAvatar(owner) {
  const next = owners.filter((entry) => entry.owner !== owner);
  if (next.length === owners.length) return;
  owners = next;
  settle();
}

export function getSharedAvatar() { return current; }

function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useSharedAvatar() {
  return useSyncExternalStore(subscribe, getSharedAvatar, getSharedAvatar);
}

/** Test seam. */
export function resetAvatarPresence() {
  owners = [];
  settle();
}
