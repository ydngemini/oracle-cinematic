import { Suspense, lazy, useState } from 'react';

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
 * Three ways to end up flat rather than 3D, all of them ending in the same
 * place — the mascot is still there, just not rendered:
 *   · the viewer asked for reduced motion, or is on a low-power / save-data
 *     device (`hasHighMotionBudget` covers cores, memory and saveData)
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

  // Read once per render, not stored: the answer depends on the OS media
  // query, which can change under the viewer mid-session.
  const wants3D = !policy.reduced && canRender3D() && !failed;

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
