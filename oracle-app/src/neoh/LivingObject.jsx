import { useEffect, useState } from 'react';

import { livingLabel, livingLine, LIVING_TONE } from './livingModel';
import styles from './LivingObject.module.css';

/**
 * LivingStrip — the part of a person card that changes with their real state.
 *
 * Resting states (dormant, quiet) render almost nothing: a dim word and a
 * time. The strip only "wakes up" when something is actually happening —
 * a call, a contract, a week of signals — so prominence on the screen tracks
 * prominence in the agent's day, and nothing screams.
 *
 * While a call is live the line carries a running duration, which is the one
 * place this component keeps its own clock.
 */
export function LivingStrip({ living, compact = false }) {
  const ticking = living?.state === 'calling';
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return undefined;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [ticking]);

  if (!living?.state) return null;
  const tone = LIVING_TONE[living.state] || 'neutral';
  const line = livingLine(living, now);
  return (
    <span
      className={`${styles.strip} ${compact ? styles.compact : ''}`}
      data-tone={tone}
      data-state={living.state}
      role="status"
      aria-live={tone === 'call' ? 'polite' : 'off'}
    >
      <span className={styles.dot} aria-hidden="true" />
      <span className={styles.label}>{livingLabel(living.state)}</span>
      {line && <span className={styles.line}>{line}</span>}
    </span>
  );
}
