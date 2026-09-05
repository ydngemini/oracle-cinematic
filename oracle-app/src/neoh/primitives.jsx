import { lazy } from 'react';

import { ConfidenceMeter } from '../components/IntelligenceFeed';
import { LivingStrip } from './LivingObject';
import styles from './registry.module.css';

/**
 * primitives — the shapes Neoh is allowed to draw.
 *
 * The generative half of this product generates ARRANGEMENT, never markup.
 * A model that could emit HTML could emit a fabricated number inside a
 * heading, and once rendered that is indistinguishable from a real one. So
 * the backend returns `{primitive, props}` from a closed list, and this file
 * is the only place that decides what those become on screen.
 *
 * A primitive this file does not know is skipped with one console warning —
 * never a blank panel, and never a thrown error that takes the rest of the
 * answer down with it. That is the failure mode a fixed vocabulary is for:
 * a version skew between the two halves costs one block, not the screen.
 */

export const OpportunityBlock = lazy(() =>
  import('../components/IntelligenceFeed').then((m) => ({ default: m.OpportunityCard })));

/* ── Small primitives ───────────────────────────────────────────────────── */

function currency(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  return n.toLocaleString(undefined, {
    style: 'currency', currency: 'USD', maximumFractionDigits: 0,
  });
}

function timeWord(iso) {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  const days = Math.round((at.getTime() - Date.now()) / 86_400_000);
  if (days === 0) return 'today';
  if (days === 1) return 'tomorrow';
  if (days === -1) return 'yesterday';
  if (days < 0) return `${-days}d ago`;
  if (days < 7) return `in ${days}d`;
  return at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function Metric({ label, value, unit, caveat, calibrated }) {
  const shown = unit === 'currency' ? currency(value) : value;
  if (shown === null || shown === undefined) return null;
  return (
    <div className={styles.metric}>
      <span className={styles.metricLabel}>{label}</span>
      <strong className={styles.metricValue}>{shown}</strong>
      {/* The caveat is rendered with the number, never beside it or below the
          fold. An uncertain figure shown bare is the false precision the
          expected-value module exists to refuse. */}
      {caveat && (
        <span className={styles.metricCaveat}>
          {!calibrated && <em className={styles.stillLearning}>Still learning. </em>}
          {caveat}
        </span>
      )}
    </div>
  );
}

export function CallQueue({ items, onOpen, onAct }) {
  if (!Array.isArray(items) || items.length === 0) return null;
  return (
    <ol className={styles.queue}>
      {items.map((item) => (
        <li key={`${item.subject_type}-${item.subject_id}-${item.rank}`} className={styles.queueItem}>
          <span className={styles.rank} aria-hidden="true">{item.rank}</span>
          <div className={styles.queueBody}>
            <button
              type="button"
              className={styles.queueName}
              onClick={() => item.href && onOpen?.(item.href)}
              disabled={!item.href}
            >
              {item.subject}
            </button>
            <span className={styles.queueWhy}>{item.headline || item.why}</span>
            <span className={styles.queueMeta}>
              {item.deadline && <span>{timeWord(item.deadline)}</span>}
              {typeof item.confidence === 'number' && <ConfidenceMeter value={item.confidence} />}
            </span>
          </div>
          {item.action && (
            <button type="button" className={styles.queueAction} onClick={() => onAct?.(item)}>
              {item.action_type === 'call' ? 'Call' : 'Do it'}
            </button>
          )}
        </li>
      ))}
    </ol>
  );
}

export function Comparison({ title, options, onOpen }) {
  if (!Array.isArray(options) || options.length === 0) return null;
  return (
    <div className={styles.comparison}>
      {title && <h3 className={styles.comparisonTitle}>{title}</h3>}
      <ul className={styles.options}>
        {options.map((option) => (
          <li key={option.id}>
            <button
              type="button"
              className={styles.option}
              onClick={() => option.href && onOpen?.(option.href)}
              disabled={!option.href}
            >
              <span className={styles.optionLabel}>{option.label}</span>
              {option.detail && <span className={styles.optionDetail}>{option.detail}</span>}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function EntityCard({ kind, name, address, subtitle, detail, read, confidence, status,
                      openCount, totalCount, href, onOpen, living = null }) {
  const title = name || address || 'Record';
  const sub = subtitle || detail
    || (status && [status.replace(/_/g, ' '), openCount != null ? `${openCount} of ${totalCount} open` : null]
      .filter(Boolean).join(' · '));
  return (
    <button
      type="button"
      className={styles.entity}
      onClick={() => href && onOpen?.(href)}
      disabled={!href}
      data-kind={kind}
      data-living={living?.state || undefined}
    >
      <span className={styles.entityTitle}>{title}</span>
      {living && <LivingStrip living={living} compact />}
      {sub && <span className={styles.entitySub}>{sub}</span>}
      {read && <span className={styles.entityRead}>{read}</span>}
      {typeof confidence === 'number' && <ConfidenceMeter value={confidence} />}
    </button>
  );
}

export function Timeline({ items }) {
  if (!Array.isArray(items) || items.length === 0) return null;
  return (
    <ul className={styles.timeline}>
      {items.map((item, i) => (
        <li key={`${item.label}-${i}`} className={styles.timelineItem} data-done={item.done ? 'true' : 'false'}>
          <span className={styles.timelineLabel}>{item.label}</span>
          {item.at && <span className={styles.timelineAt}>{timeWord(item.at)}</span>}
        </li>
      ))}
    </ul>
  );
}

export function Evidence({ items }) {
  if (!Array.isArray(items) || items.length === 0) return null;
  return (
    <ul className={styles.evidence}>
      {items.map((item, i) => (
        <li key={`${item.label}-${i}`}>
          <span className={styles.evidenceLabel}>{item.label}</span>
          {item.detail && <span className={styles.evidenceDetail}>{item.detail}</span>}
        </li>
      ))}
    </ul>
  );
}
