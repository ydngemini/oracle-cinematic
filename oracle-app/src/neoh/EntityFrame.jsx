import { useEffect, useRef } from 'react';

import { LivingStrip } from './LivingObject';
import { NeohRead } from './NeohRead';
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
  facts, actions, onClose, children,
}) {
  const sheetRef = useSheetChrome(onClose);

  return (
    <div className={styles.layer}>
      <button
        type="button"
        className={styles.scrim}
        aria-label={`Close ${kind}`}
        onClick={onClose}
      />
      <section
        className={styles.sheet}
        role="dialog"
        aria-modal="true"
        aria-label={`${kind} — ${title || 'record'}`}
        ref={sheetRef}
        tabIndex={-1}
      >
        <header className={styles.head}>
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
          <button type="button" className={styles.close} onClick={onClose} aria-label="Close">×</button>
        </header>

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
      </section>
    </div>
  );
}

/** Escape closes; focus lands in the sheet on open and returns on close. */
function useSheetChrome(onClose) {
  const sheetRef = useRef(null);
  useEffect(() => {
    const opener = document.activeElement;
    const frame = window.requestAnimationFrame(() => sheetRef.current?.focus());
    const onKey = (event) => {
      if (event.key === 'Escape') { event.stopPropagation(); onClose?.(); }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener('keydown', onKey);
      if (opener && typeof opener.focus === 'function') opener.focus();
    };
  }, [onClose]);
  return sheetRef;
}
