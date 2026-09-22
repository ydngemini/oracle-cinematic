import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Calendar, Home, MessageSquare, Phone, Save, Search, Share2, Sparkles } from 'lucide-react';

import { useMotionPolicy } from './motion';
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

/** Eye shape per state. The face is two eyes and a light — that is enough. */
function eyeGeometry(state) {
  switch (state) {
    case 'thinking':
      // Looking up and inward — concentration, not a spinner on a face.
      return { cx: [12.4, 19.6], cy: [13.4, 13.4], rx: 2.1, ry: 2.4, tilt: -8 };
    case 'listening':
      return { cx: [12, 20], cy: [14.6, 14.6], rx: 2.5, ry: 2.8, tilt: 0 };
    case 'speaking':
      return { cx: [12, 20], cy: [14.4, 14.4], rx: 2.4, ry: 2.5, tilt: 0 };
    case 'success':
      // The established "happy arc" eyes from the character sheet.
      return { cx: [12, 20], cy: [14.6, 14.6], rx: 2.6, ry: 1.2, tilt: 0, arc: true };
    case 'needs_attention':
      return { cx: [12, 20], cy: [14.0, 14.0], rx: 2.7, ry: 3.0, tilt: 0 };
    case 'error':
      return { cx: [12, 20], cy: [15.0, 15.0], rx: 2.3, ry: 1.7, tilt: 0 };
    case 'disconnected':
      return { cx: [12, 20], cy: [15.0, 15.0], rx: 2.2, ry: 1.4, tilt: 0 };
    case 'acting':
      return { cx: [12, 20], cy: [14.2, 14.2], rx: 2.4, ry: 2.6, tilt: 0 };
    default:
      return { cx: [12, 20], cy: [14.6, 14.6], rx: 2.4, ry: 2.6, tilt: 0 };
  }
}

function FallbackFace({ state, level, reduced }) {
  const eyes = eyeGeometry(state);
  // The accent ring is the one thing amplitude drives. Driving the eyes with
  // audio reads as a mouth, and a mouth on this character is uncanny.
  const ring = reduced ? 0 : Math.min(1, Math.max(0, level));

  return (
    <svg
      viewBox="0 0 32 32"
      className={styles.svg}
      focusable="false"
      aria-hidden="true"
      style={{ '--neoh-level': ring.toFixed(3) }}
    >
      {/* Roof silhouette — the home cue, carried in the head outline. */}
      <path className={styles.roof} d="M16 2.6 4.6 11.2v12.2A4.2 4.2 0 0 0 8.8 27.6h14.4a4.2 4.2 0 0 0 4.2-4.2V11.2Z" />
      {/* Dark face panel. */}
      <path className={styles.face} d="M16 6.4 8 12.4v9.8a2.4 2.4 0 0 0 2.4 2.4h11.2a2.4 2.4 0 0 0 2.4-2.4v-9.8Z" />
      {/* Side lights — these are what respond to amplitude. */}
      <rect className={styles.earLeft} x="3.2" y="13.4" width="2.2" height="6.2" rx="1.1" />
      <rect className={styles.earRight} x="26.6" y="13.4" width="2.2" height="6.2" rx="1.1" />
      <g className={styles.eyes} transform={`rotate(${eyes.tilt} 16 15)`}>
        {eyes.arc ? (
          <>
            <path className={styles.eyeArc} d={`M${eyes.cx[0] - eyes.rx} ${eyes.cy[0] + 0.6}q${eyes.rx} -2.4 ${eyes.rx * 2} 0`} />
            <path className={styles.eyeArc} d={`M${eyes.cx[1] - eyes.rx} ${eyes.cy[1] + 0.6}q${eyes.rx} -2.4 ${eyes.rx * 2} 0`} />
          </>
        ) : (
          <>
            <ellipse className={styles.eye} cx={eyes.cx[0]} cy={eyes.cy[0]} rx={eyes.rx} ry={eyes.ry} />
            <ellipse className={styles.eye} cx={eyes.cx[1]} cy={eyes.cy[1]} rx={eyes.rx} ry={eyes.ry} />
          </>
        )}
      </g>
    </svg>
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
  const level = still ? 0 : audioLevel;

  return (
    <span
      className={`${styles.avatar} ${className}`}
      data-state={state}
      data-variant={variant}
      data-still={still ? 'true' : 'false'}
      style={{ '--neoh-avatar-size': size, '--neoh-attention': String(attentionLevel || 0) }}
      aria-hidden="true"
    >
      {NeohAvatarRive && !riveFailed ? (
        <Suspense fallback={<FallbackFace state={state} level={level} reduced={still} />}>
          <NeohAvatarRive
            src={RIVE_SRC}
            state={state}
            audioLevel={level}
            actionType={actionType}
            attentionLevel={attentionLevel}
            still={still}
            onError={() => setRiveFailed(true)}
          />
        </Suspense>
      ) : (
        <FallbackFace state={state} level={level} reduced={still} />
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
