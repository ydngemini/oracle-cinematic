/**
 * A rare, autonomous look-away.
 *
 * Deliberately NOT pointer tracking. Eyes that follow the cursor around the
 * screen read as surveillance rather than life, and they are the fastest way
 * to turn a calm character into a gimmick. This drifts on its own schedule
 * and has no idea where anyone is.
 *
 * It lives outside React as a tiny external store for two reasons. Ticking it
 * with setState inside an effect is the pattern React 19 rightly rejects, and
 * one shared clock means ten avatars cost one timer rather than ten — while
 * also keeping them in agreement, which is what you want from one character
 * rendered twice on the same screen.
 *
 * The timer only exists while something is subscribed, so an app with no
 * bust-sized Neoh on screen pays nothing at all.
 */

const listeners = new Set();

/** @type {{gazeX: number, gazeY: number} | null} */
let current = null;
let timer = 0;

/** Minutes-scale, not seconds-scale: this is punctuation, not motion. */
const REST_MIN_MS = 6_000;
const REST_JITTER_MS = 9_000;
const GLANCE_MS = 1_100;

function emit() {
  for (const fn of listeners) fn();
}

function scheduleGlance() {
  timer = window.setTimeout(() => {
    current = {
      gazeX: (Math.random() - 0.5) * 1.1,
      gazeY: (Math.random() - 0.5) * 0.7,
    };
    emit();
    timer = window.setTimeout(() => {
      current = null;
      emit();
      scheduleGlance();
    }, GLANCE_MS);
  }, REST_MIN_MS + Math.random() * REST_JITTER_MS);
}

export function subscribeIdleGaze(onChange) {
  listeners.add(onChange);
  if (listeners.size === 1) scheduleGlance();
  return () => {
    listeners.delete(onChange);
    if (listeners.size === 0) {
      window.clearTimeout(timer);
      timer = 0;
      current = null;
    }
  };
}

/**
 * Stable between changes, which `useSyncExternalStore` requires — returning a
 * fresh object each call would spin the render loop.
 */
export function getIdleGaze() {
  return current;
}

/** Nothing drifts before hydration. */
export function getIdleGazeServer() {
  return null;
}

/**
 * Subscribing to nothing. Used when an avatar is too small or too busy to
 * care, so it neither renders on a tick nor keeps the shared timer alive.
 */
export function subscribeNothing() {
  return () => {};
}
