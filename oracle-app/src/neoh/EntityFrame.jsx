import { motion } from 'framer-motion';
import { useEffect, useEffectEvent, useRef } from 'react';

import { LivingStrip } from './LivingObject';
import { NeohRead } from './NeohRead';
import { useMotionPolicy } from './motion';
import styles from './EntitySheet.module.css';

/**
 * One grammar for a person, a property and a deal.
 *
 * The three sheets were three applications wearing one route. A person opened
 * the client drawer, a property opened the asset dossier — "FILE № …",
 * "DECRYPTING FILE…" — and a deal opened something built later with neither
 * of their conventions. Every one taught the reader a different object model,
 * so nothing learned on one carried to the next.
 *
 * What every entity has, in this order, always:
 *
 *   1. what it is and what it is called
 *   2. what state it is in right now        (LivingStrip)
 *   3. what Neoh makes of it                (NeohRead)
 *   4. the few numbers that describe it     (facts)
 *   5. the few things you can do to it      (actions)
 *   6. everything else                      (the panel that already existed)
 *
 * The panels themselves are not rewritten. That is deliberate: they hold real
 * work — inline editing, compliance, documents — and rewriting three of them
 * to change their headers would be a large change with nothing to show for it.
 * They keep whatever only they can do, and lose the chrome the frame now owns.
 */
export function EntityFrame({
  kind, title, subline, living, read, readLoading, readError,
  facts, actions, onClose, children, immersive = null,
}) {
  const sheetRef = useSheetChrome(onClose);
  const policy = useMotionPolicy();
  const expanded = immersive !== null;
  const closeRef = useRef(null);
  const tourOpener = useRef(null);
  const backLabel = `Back to ${String(kind || 'record').toLowerCase()}`;

  useEffect(() => {
    if (expanded) {
      tourOpener.current = document.activeElement;
      closeRef.current?.focus({ preventScroll: true });
    } else if (tourOpener.current) {
      const target = sheetRef.current?.contains(tourOpener.current) ? tourOpener.current : closeRef.current;
      target?.focus({ preventScroll: true });
      tourOpener.current = null;
    }
  }, [expanded, sheetRef]);

  return (
    <motion.div className={styles.layer} layoutRoot>
      <button
        type="button"
        className={styles.scrim}
        aria-label={expanded ? backLabel : `Close ${kind}`}
        aria-hidden={expanded || undefined}
        onClick={onClose}
        tabIndex={-1}
      />
      <motion.section
        className={`${styles.sheet} ${expanded ? styles.sheetExpanded : ''}`}
        layout={policy.layout}
        transition={policy.transition}
        role="dialog"
        aria-modal="true"
        aria-label={`${kind} — ${title || 'record'}`}
        ref={sheetRef}
        tabIndex={-1}
      >
        <motion.header className={styles.head} layout={policy.layout ? 'position' : false}>
          <div className={styles.headText}>
            <span className={styles.kicker}>{kind}</span>
            {/* Not every kind supplies a title here: the client drawer's own
                header carries an inline rename, and moving that would cost a
                real capability to gain a consistent one. It keeps its header;
                everything below is shared. */}
            {title && <h1 className={styles.title}>{title}</h1>}
            {subline?.length > 0 && (
              <span className={styles.subline}>
                {subline.filter(Boolean).map((bit) => <span key={bit}>{bit}</span>)}
              </span>
            )}
            {living && <LivingStrip living={living} />}
          </div>
          <button
            ref={closeRef}
            type="button"
            className={expanded ? styles.back : styles.close}
            onClick={onClose}
            aria-label={expanded ? backLabel : 'Close'}
          >
            {expanded ? `← ${backLabel}` : '×'}
          </button>
        </motion.header>

        <div className={styles.content}>
          <div className={`${styles.details} ${expanded ? styles.detailsHidden : ''}`} inert={expanded} aria-hidden={expanded || undefined}>
            <NeohRead read={read} loading={readLoading} error={readError} />

            {facts?.length > 0 && (
              <dl className={styles.facts}>
                {facts.map((fact) => (
                  <div key={fact.label}>
                    <dt>{fact.label}</dt>
                    <dd>{fact.value ?? '—'}</dd>
                  </div>
                ))}
              </dl>
            )}

            {actions?.length > 0 && (
              <div className={styles.actions}>
                {actions.map((action) => (
                  <button
                    key={action.label}
                    type="button"
                    className={action.primary ? styles.actionPrimary : styles.action}
                    onClick={action.onClick}
                    disabled={action.disabled}
                  >
                    {action.label}
                  </button>
                ))}
              </div>
            )}

            <div className={styles.body}>{children}</div>
          </div>
          {expanded && <div className={styles.immersive}>{immersive}</div>}
        </div>
      </motion.section>
    </motion.div>
  );
}

/** Escape closes; focus lands in the sheet on open and returns on close. */
function useSheetChrome(onClose) {
  const sheetRef = useRef(null);
  const close = useEffectEvent(() => onClose?.());
  useEffect(() => {
    const opener = document.activeElement;
    const frame = window.requestAnimationFrame(() => {
      if (!sheetRef.current?.contains(document.activeElement)) sheetRef.current?.focus({ preventScroll: true });
    });
    const onKey = (event) => {
      if (event.defaultPrevented) return;
      const activeDialog = document.activeElement?.closest('[role="dialog"]');
      if (activeDialog && activeDialog !== sheetRef.current) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        if (!event.repeat) close();
      }
      if (event.key !== 'Tab') return;
      const sheet = sheetRef.current;
      const controls = Array.from(sheet?.querySelectorAll(
        'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
      ) || []).filter((control) => !control.closest('[inert]') && control.getClientRects().length > 0);
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (!first) {
        event.preventDefault();
        sheet?.focus({ preventScroll: true });
      } else if (event.shiftKey && (document.activeElement === first || document.activeElement === sheet)) {
        event.preventDefault();
        last.focus();
      } else if (!sheet?.contains(document.activeElement) || (!event.shiftKey && document.activeElement === last)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener('keydown', onKey);
      if (opener && typeof opener.focus === 'function') opener.focus({ preventScroll: true });
    };
  }, []);
  return sheetRef;
}
