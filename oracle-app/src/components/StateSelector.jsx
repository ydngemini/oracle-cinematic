import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useStateCtx } from '../state/StateContext';
import { US_STATES } from '../lib/usStates';
import styles from './StateSelector.module.css';

const FLAG_GLYPH = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M5 4v16" />
    <path d="M5 4h11l-2 3.5 2 3.5H5" />
  </svg>
);

export function StateSelector() {
  const { activeStates, setActiveStates, addState, removeState } = useStateCtx();
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const ref = useRef(null);
  const triggerRef = useRef(null);
  const stateRefs = useRef([]);
  const sheetId = useId();

  const toggle = useCallback((code) => {
    if (activeStates.includes(code)) removeState(code);
    else addState(code);
  }, [activeStates, addState, removeState]);

  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    const keyboard = (e) => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener('pointerdown', handler);
    document.addEventListener('keydown', keyboard);
    return () => {
      document.removeEventListener('pointerdown', handler);
      document.removeEventListener('keydown', keyboard);
    };
  }, [open]);

  const filtered = filter
    ? US_STATES.filter(({ code, name }) =>
        code.toLowerCase().includes(filter.toLowerCase()) ||
        name.toLowerCase().includes(filter.toLowerCase()))
    : US_STATES;

  return (
    <div className={styles.wrap} ref={ref}>
      <button
        ref={triggerRef}
        type="button"
        className={styles.pill}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={open ? sheetId : undefined}
        aria-haspopup="dialog"
        aria-label={`Select active jurisdictions. ${activeStates.length === 0 ? 'All 50 states and DC active' : `${activeStates.length} selected`}`}
      >
        <span className={styles.pillGlyph}>{FLAG_GLYPH}</span>
        <span className={styles.pillLabel}>
          {activeStates.length === 0
            ? 'All 50 + DC'
            : activeStates.length <= 3
              ? activeStates.join(', ')
              : `${activeStates.length} states`}
        </span>
      </button>

      {open && (
        <div id={sheetId} className={styles.sheet} role="dialog" aria-label="State selector">
          <input
            className={styles.search}
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Search states…"
            aria-label="Search states"
            autoFocus
          />
          <div className={styles.grid} role="group" aria-label="Available states">
            {filtered.map(({ code, name }, index) => {
              const selected = activeStates.includes(code);
              return (
                <button
                  key={code}
                  ref={(node) => { stateRefs.current[index] = node; }}
                  type="button"
                  className={`${styles.stateBtn} ${selected ? styles.stateBtnActive : ''}`}
                  onClick={() => toggle(code)}
                  onKeyDown={(event) => {
                    const columns = 5;
                    const delta = event.key === 'ArrowRight' ? 1
                      : event.key === 'ArrowLeft' ? -1
                        : event.key === 'ArrowDown' ? columns
                          : event.key === 'ArrowUp' ? -columns : 0;
                    if (!delta) return;
                    event.preventDefault();
                    const next = Math.max(0, Math.min(filtered.length - 1, index + delta));
                    stateRefs.current[next]?.focus();
                  }}
                  aria-pressed={selected}
                  aria-label={`${name} (${code})`}
                  title={name}
                >
                  {code}
                </button>
              );
            })}
          </div>
          <div className={styles.footer}>
            {activeStates.length > 0 ? (
              <button type="button" className={styles.clearBtn} onClick={() => setActiveStates([])}>
                Use all states
              </button>
            ) : null}
            {activeStates.length > 0 && (
              <span className={styles.footerCount}>{activeStates.length} selected</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
