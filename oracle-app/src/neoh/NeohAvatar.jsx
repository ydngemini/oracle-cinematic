import { lazy, Suspense, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Calendar, Home, MessageSquare, Phone, Save, Search, Share2, Sparkles } from 'lucide-react';

import { getIdleGaze, getIdleGazeServer, subscribeIdleGaze, subscribeNothing } from './idleGaze';
import { useMotionPolicy } from './motion';
import { NeohCharacter } from './NeohCharacter';
import { eyeExpression } from './eyeSystem';
import styles from './NeohAvatar.module.css';

/**
 * NeohAvatar — Neoh's face, at icon scale.
 *
 * The rest of the product speaks product states to this component (idle,
 * listening, thinking, speaking, acting…) and never learns an animation
 * input name. That boundary is the whole point: swapping the renderer for a
 * professionally animated asset must not touch a single caller.
 *
 * Two renderers sit behind the same props:
 *
 *   fallback (here) — inline SVG: dark face panel, cyan eyes, roof
 *                     silhouette. Ships today, weighs nothing, and is a real
 *                     implementation rather than a placeholder box.
 *   rive            — lazy, and only when an asset is actually present.
 *                     See NeohAvatarRive.jsx for the input contract.
 *
 * Idle is close to motionless on purpose. Neoh should be quiet at rest and
 * extraordinary when something moves; an avatar that pulses forever trains
 * the eye to ignore it, which costs the states that matter.
 */

const RIVE_SRC = import.meta.env?.VITE_NEOH_AVATAR_RIVE || '';

const NeohAvatarRive = RIVE_SRC ? lazy(() => import('./NeohAvatarRive.jsx')) : null;

const ACTION_GLYPHS = {
  call: Phone,
  message: MessageSquare,
  calendar: Calendar,
  property: Home,
  search: Search,
  save: Save,
  share: Share2,
  generic: Sparkles,
};

function FallbackFace({ state, variant, drift }) {
  return <NeohCharacter variant={variant} expression={eyeExpression(state)} drift={drift} />;
}

/**
 * Subscribe to the shared idle drift, but only when it would be visible.
 *
 * At 20px a 0.3px eye offset is invisible, and any state other than idle has
 * something to say that drift would only blur — so those cases subscribe to
 * nothing and are never re-rendered by the clock.
 */
function useIdleGaze(active) {
  return useSyncExternalStore(
    active ? subscribeIdleGaze : subscribeNothing,
    active ? getIdleGaze : getIdleGazeServer,
    getIdleGazeServer,
  );
}

/**
 * @param {object} props
 * @param {string} [props.state]        semantic state from avatarModel
 * @param {number} [props.audioLevel]   0..1, already smoothed
 * @param {string} [props.actionType]   glyph category while acting
 * @param {number} [props.attentionLevel] 0..1
 * @param {'head'|'bust'|'full'} [props.variant]
 * @param {string} [props.size]         any CSS length
 */
export function NeohAvatar({
  state = 'idle',
  audioLevel = 0,
  actionType = 'generic',
  attentionLevel = 0,
  variant = 'head',
  size = '20px',
  className = '',
}) {
  const policy = useMotionPolicy();
  const [riveFailed, setRiveFailed] = useState(false);
  const hidden = usePageHidden();

  // A hidden tab must not keep an animation loop warm. Reduced motion and a
  // hidden page take the same path: the state still renders, it just holds.
  const still = policy.reduced || hidden;
  const Glyph = state === 'acting' ? (ACTION_GLYPHS[actionType] || ACTION_GLYPHS.generic) : null;
  const level = still ? 0 : Math.min(1, Math.max(0, audioLevel));
  const drift = useIdleGaze(variant !== 'head' && state === 'idle' && !still);

  return (
    <span
      className={`${styles.avatar} ${className}`}
      data-state={state}
      data-variant={variant}
      data-still={still ? 'true' : 'false'}
      style={{
        '--neoh-avatar-size': size,
        // A level, not a colour — `--neoh-attention` is the attention HUE in
        // the stylesheet, and conflating the two makes every rule ambiguous.
        '--neoh-attention-level': String(attentionLevel || 0),
        // Amplitude lands on a custom property, never on geometry props. The
        // browser can then animate glow and scale off the compositor without
        // React touching the SVG on every audio frame.
        '--neoh-level': level.toFixed(3),
      }}
      aria-hidden="true"
    >
      {NeohAvatarRive && !riveFailed ? (
        <Suspense fallback={<FallbackFace state={state} variant={variant} drift={drift} />}>
          <NeohAvatarRive
            src={RIVE_SRC}
            state={state}
            audioLevel={level}
            actionType={actionType}
            attentionLevel={attentionLevel}
            still={still}
            variant={variant}
            onError={() => setRiveFailed(true)}
          />
        </Suspense>
      ) : (
        <FallbackFace state={state} variant={variant} drift={drift} />
      )}
      {Glyph ? (
        <span className={styles.glyph}>
          <Glyph size={10} aria-hidden="true" />
        </span>
      ) : null}
    </span>
  );
}

/** True while the tab is hidden, so nothing animates off-screen. */
function usePageHidden() {
  const [hidden, setHidden] = useState(() => (typeof document === 'undefined' ? false : document.hidden));
  const frame = useRef(0);
  useEffect(() => {
    if (typeof document === 'undefined') return undefined;
    const onChange = () => {
      window.cancelAnimationFrame(frame.current);
      frame.current = window.requestAnimationFrame(() => setHidden(document.hidden));
    };
    document.addEventListener('visibilitychange', onChange);
    return () => {
      window.cancelAnimationFrame(frame.current);
      document.removeEventListener('visibilitychange', onChange);
    };
  }, []);
  return hidden;
}

export default NeohAvatar;
