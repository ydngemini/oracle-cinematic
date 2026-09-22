import { VARIANT_GEOMETRY } from './characterGeometry';
import { eyeGeometry } from './eyeSystem';
import styles from './NeohAvatar.module.css';

/**
 * The Neoh character, as a layered vector rig.
 *
 * Every part that has to move is its own node with its own transform origin.
 * That is the whole reason this is not one clever path: a single path cannot
 * tilt a head without dragging the shoulders, and cannot pulse a chest light
 * without pulsing the torso around it.
 *
 * Three variants share one rig and one coordinate convention — the head is
 * always drawn in its own 32x32 space and placed by a transform, so the head
 * geometry is written once and the bust and full body inherit every fix to it.
 *
 *   head  32 x 32   the product default: pills, inputs, status
 *   bust  40 x 44   voice and conversation, where expression has room
 *   full  40 x 72   onboarding, empty states, a finished setup
 *
 * Nothing here decides anything. It is handed a state and a level and draws
 * them; avatarModel decides what is true and useNeohAvatarState decides when.
 */


/* ── Head ───────────────────────────────────────────────────────────────── */

function Eyes({ geo }) {
  return (
    <g className={styles.eyes} transform={`rotate(${geo.tilt} 16 15)`}>
      {geo.arc ? (
        <>
          <path className={styles.eyeArc} d={`M${geo.cx[0] - geo.rx} ${geo.cy[0] + 0.6}q${geo.rx} -2.4 ${geo.rx * 2} 0`} />
          <path className={styles.eyeArc} d={`M${geo.cx[1] - geo.rx} ${geo.cy[1] + 0.6}q${geo.rx} -2.4 ${geo.rx * 2} 0`} />
        </>
      ) : (
        <>
          <ellipse className={styles.eye} cx={geo.cx[0]} cy={geo.cy[0]} rx={geo.rx} ry={geo.ry} />
          <ellipse className={styles.eye} cx={geo.cx[1]} cy={geo.cy[1]} rx={geo.rx} ry={geo.ry} />
        </>
      )}
    </g>
  );
}

/**
 * The circular side modules. Amplitude drives these and never the eyes: a
 * light that tracks speech reads as a voice, whereas eyes that do read as a
 * mouth — and this character does not have one.
 */
function SideModule({ side, cx }) {
  const cls = side === 'left' ? styles.sideLeft : styles.sideRight;
  return (
    <g className={cls}>
      <circle className={styles.sideRing} cx={cx} cy="16.4" r="2.6" />
      <circle className={styles.sideCore} cx={cx} cy="16.4" r="1.15" />
    </g>
  );
}

/**
 * The head, in its own 32x32 space.
 *
 * `roofEdge` is a separate stroked polyline rather than the shell's own
 * stroke, so the roofline can light up and travel without the whole
 * silhouette changing weight.
 */
function Head({ geo }) {
  return (
    <g className={styles.head}>
      <path
        className={styles.shell}
        d="M16 2.6 4.6 11.2v12.2A4.2 4.2 0 0 0 8.8 27.6h14.4a4.2 4.2 0 0 0 4.2-4.2V11.2Z"
      />
      <path className={styles.roofEdge} d="M4.6 11.2 16 2.6l11.4 8.6" />
      <path
        className={styles.visor}
        d="M16 6.4 8 12.4v9.8a2.4 2.4 0 0 0 2.4 2.4h11.2a2.4 2.4 0 0 0 2.4-2.4v-9.8Z"
      />
      <SideModule side="left" cx="3.4" />
      <SideModule side="right" cx="28.6" />
      <Eyes geo={geo} />
    </g>
  );
}

/* ── Body ───────────────────────────────────────────────────────────────── */

/** The home mark on the chest — a key identity cue, so it is never decoration. */
function ChestEmblem({ x, y, scale = 1 }) {
  return (
    <g className={styles.emblem} transform={`translate(${x} ${y}) scale(${scale})`}>
      <path className={styles.emblemShape} d="M0 0-4.4 3.6V9a1.3 1.3 0 0 0 1.3 1.3h6.2A1.3 1.3 0 0 0 4.4 9V3.6Z" />
    </g>
  );
}

function Arm({ side, x, y, length, className }) {
  const dir = side === 'left' ? -1 : 1;
  return (
    <g className={className}>
      <circle className={styles.joint} cx={x} cy={y} r="1.9" />
      <rect
        className={styles.limb}
        x={x - 1.55} y={y} width="3.1" height={length} rx="1.55"
        transform={`rotate(${dir * -7} ${x} ${y})`}
      />
      <circle className={styles.hand} cx={x + dir * 0.9} cy={y + length + 0.4} r="1.8" />
    </g>
  );
}

/** Head, neck, shoulders and chest. The expressive form for voice. */
function BustBody() {
  return (
    <g className={styles.body}>
      <rect className={styles.neck} x="17" y="21.5" width="6" height="4.5" rx="2.2" />
      <path
        className={styles.torso}
        d="M20 23c-6.6 0-11.4 4.3-11.4 10.2V44h22.8v-10.8C31.4 27.3 26.6 23 20 23Z"
      />
      <ChestEmblem x="20" y="31.4" scale="0.42" />
      <Arm side="left" x="9.4" y="30.5" length={11} className={styles.armLeft} />
      <Arm side="right" x="30.6" y="30.5" length={11} className={styles.armRight} />
    </g>
  );
}

/** The whole character. Reserved for moments, never a panel decoration. */
function FullBody() {
  return (
    <g className={styles.body}>
      <rect className={styles.neck} x="17.4" y="20" width="5.2" height="3.8" rx="1.9" />
      <path
        className={styles.torso}
        d="M20 22c-5.6 0-9.6 3.6-9.6 8.6v12.6c0 2.7 2.1 4.6 4.8 4.6h9.6c2.7 0 4.8-1.9 4.8-4.6V30.6c0-5-4-8.6-9.6-8.6Z"
      />
      <ChestEmblem x="20" y="29.6" scale="0.42" />
      <Arm side="left" x="11" y="28" length={13} className={styles.armLeft} />
      <Arm side="right" x="29" y="28" length={13} className={styles.armRight} />
      {/* Legs: two rounded columns and feet. Simple on purpose — an animated
          walk cycle is not something this character is ever asked to do. */}
      <g className={styles.legs}>
        <rect className={styles.limb} x="14.8" y="46.6" width="4.4" height="13.6" rx="2.2" />
        <rect className={styles.limb} x="20.8" y="46.6" width="4.4" height="13.6" rx="2.2" />
        <ellipse className={styles.foot} cx="17" cy="62" rx="3.4" ry="2.4" />
        <ellipse className={styles.foot} cx="23" cy="62" rx="3.4" ry="2.4" />
      </g>
    </g>
  );
}

/**
 * @param {object} props
 * @param {'head'|'bust'|'full'} props.variant
 * @param {object} props.expression  from eyeSystem
 * @param {{gazeX?: number, gazeY?: number}} [props.drift] autonomous micro-gaze
 */
export function NeohCharacter({ variant = 'head', expression, drift }) {
  const shape = VARIANT_GEOMETRY[variant] || VARIANT_GEOMETRY.head;
  const geo = eyeGeometry(expression, drift);
  const head = <Head geo={geo} />;

  return (
    <svg viewBox={shape.viewBox} className={styles.svg} focusable="false" aria-hidden="true">
      {variant === 'bust' && <BustBody />}
      {variant === 'full' && <FullBody />}
      {shape.headTransform ? <g transform={shape.headTransform}>{head}</g> : head}
    </svg>
  );
}

export default NeohCharacter;
