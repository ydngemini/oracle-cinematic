import { Suspense, lazy, useEffect, useState } from 'react';

import { NeohAvatar } from './NeohAvatar';
import { useMotionPolicy } from './motion';
import styles from './NeohCorner.module.css';

/**
 * Neoh in the corner of the home page — the cheap half.
 *
 * This file decides whether the 3D Neoh is worth loading and never imports
 * the engine itself, so the decision costs nothing on a machine that answers
 * "no". The heavy renderer is a separate lazy chunk which then pulls
 * PlayCanvas at runtime.
 *
 * The flat SVG Neoh is the default and what almost every visit sees. The 3D
 * one is fetched only after Home has sat open and visible for a while (see
 * CORNER_3D_DWELL_MS) — never on first paint, never on a phone or tablet
 * (the corner is hidden there), never on save-data or a slow connection.
 *
 * Ways to end up flat rather than 3D, all of them ending in the same
 * place — the mascot is still there, just not rendered:
 *   · the dwell has not elapsed, the corner is hidden at this width, or the
 *     connection is slow / save-data
 *   · the viewer asked for reduced motion, or is on a low-power device
 *   · the chunk or the engine failed to load
 *   · WebGL is unavailable, or the context was lost
 *
 * Reduced motion means do not animate. It has never meant hide the character,
 * so the fallback is the real mascot at corner scale, held still.
 */

const NeohCorner3D = lazy(() => import('./NeohCorner3D.jsx'));

/**
 * Whether this machine should be asked to render a 3D character.
 *
 * Deliberately NOT `hasHighMotionBudget()`, which the view-transition code
 * uses: that demands more than four cores, and a four-core laptop is an
 * ordinary machine, not a weak one — borrowing that gate here hid the mascot
 * from most of the devices it was built for. What actually matters for a
 * 176px low-power canvas is that the viewer has not asked for less motion or
 * less data, and that the device is not genuinely tiny.
 */
function canRender3D() {
  if (typeof window === 'undefined') return false;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return false;
  if (navigator.connection?.saveData === true) return false;
  if ((navigator.hardwareConcurrency || 8) < 4) return false;
  if ((navigator.deviceMemory || 8) < 4) return false;
  return true;
}

/**
 * Whether the corner is on screen at all. Below the tablet breakpoint the
 * stylesheet hides it — and the 3D chunk used to load anyway, so every phone
 * that opened Home paid ~2.3 MB of PlayCanvas for a corner it never showed.
 */
function cornerShown() {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(min-width: 1100px)').matches;
}

/** A connection that has not asked to be spared, and is not slow. */
function networkAllows3D() {
  const connection = typeof navigator === 'undefined' ? null : navigator.connection;
  if (!connection) return true;
  if (connection.saveData === true) return false;
  return !['slow-2g', '2g', '3g'].includes(connection.effectiveType);
}

/**
 * How long Home has to sit open before the 3D Neoh is worth fetching.
 *
 * Home must not download the 3D engine to be useful: opening it to read three
 * lines and leave should cost three lines. The SVG Neoh is the mascot; the
 * 3D one is an upgrade a person who keeps Home open earns, fetched only
 * after this long, only while the page is visible, only when the browser is
 * idle, and only on a machine, screen and connection that can afford it.
 */
const CORNER_3D_DWELL_MS = 30_000;

function useDwell(eligible) {
  const [awake, setAwake] = useState(false);
  useEffect(() => {
    if (!eligible) return undefined;
    let idle = 0;
    const wake = () => {
      if (typeof window.requestIdleCallback === 'function') {
        idle = window.requestIdleCallback(() => setAwake(true), { timeout: 5_000 });
      } else {
        setAwake(true);
      }
    };
    const onVisible = () => {
      if (document.hidden) return;
      document.removeEventListener('visibilitychange', onVisible);
      wake();
    };
    const timer = window.setTimeout(() => {
      if (document.hidden) document.addEventListener('visibilitychange', onVisible);
      else wake();
    }, CORNER_3D_DWELL_MS);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
      if (idle && typeof window.cancelIdleCallback === 'function') window.cancelIdleCallback(idle);
    };
  }, [eligible]);
  return awake;
}

function FlatNeoh() {
  return (
    <span className={styles.flat}>
      <NeohAvatar state="idle" />
    </span>
  );
}

export function NeohCorner() {
  const policy = useMotionPolicy();
  const [failed, setFailed] = useState(false);
  // Decided once at mount: whether this visit could ever earn the 3D Neoh.
  const [eligible] = useState(() => canRender3D() && cornerShown() && networkAllows3D());
  const awake = useDwell(eligible && !policy.reduced);

  // Reduced motion is re-read every render: the OS preference can change
  // under the viewer mid-session, and it must win immediately.
  const wants3D = awake && !policy.reduced && !failed;

  return (
    <div className={styles.corner} aria-hidden="true">
      {wants3D ? (
        <Suspense fallback={<FlatNeoh />}>
          <NeohCorner3D onFailure={() => setFailed(true)} />
        </Suspense>
      ) : (
        <FlatNeoh />
      )}
    </div>
  );
}

export default NeohCorner;
