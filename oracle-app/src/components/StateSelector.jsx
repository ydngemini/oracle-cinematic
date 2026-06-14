import { useCallback, useEffect, useRef, useState } from 'react';
import { useStateCtx } from '../state/StateContext';
import styles from './StateSelector.module.css';

const US_STATES = [
  ['AL','Alabama'],['AK','Alaska'],['AZ','Arizona'],['AR','Arkansas'],
  ['CA','California'],['CO','Colorado'],['CT','Connecticut'],['DE','Delaware'],
  ['FL','Florida'],['GA','Georgia'],['HI','Hawaii'],['ID','Idaho'],
  ['IL','Illinois'],['IN','Indiana'],['IA','Iowa'],['KS','Kansas'],
  ['KY','Kentucky'],['LA','Louisiana'],['ME','Maine'],['MD','Maryland'],
  ['MA','Massachusetts'],['MI','Michigan'],['MN','Minnesota'],['MS','Mississippi'],
  ['MO','Missouri'],['MT','Montana'],['NE','Nebraska'],['NV','Nevada'],
  ['NH','New Hampshire'],['NJ','New Jersey'],['NM','New Mexico'],['NY','New York'],
  ['NC','North Carolina'],['ND','North Dakota'],['OH','Ohio'],['OK','Oklahoma'],
  ['OR','Oregon'],['PA','Pennsylvania'],['RI','Rhode Island'],['SC','South Carolina'],
  ['SD','South Dakota'],['TN','Tennessee'],['TX','Texas'],['UT','Utah'],
  ['VT','Vermont'],['VA','Virginia'],['WA','Washington'],['WV','West Virginia'],
  ['WI','Wisconsin'],['WY','Wyoming'],
];

const FLAG_GLYPH = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M5 4v16" />
    <path d="M5 4h11l-2 3.5 2 3.5H5" />
  </svg>
);

export function StateSelector() {
  const { activeStates, addState, removeState } = useStateCtx();
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const ref = useRef(null);

  const toggle = useCallback((code) => {
    if (activeStates.includes(code)) removeState(code);
    else addState(code);
  }, [activeStates, addState, removeState]);

  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', handler);
    return () => document.removeEventListener('pointerdown', handler);
  }, [open]);

  const filtered = filter
    ? US_STATES.filter(([code, name]) =>
        code.toLowerCase().includes(filter.toLowerCase()) ||
        name.toLowerCase().includes(filter.toLowerCase()))
    : US_STATES;

  return (
    <div className={styles.wrap} ref={ref}>
      <button
        type="button"
        className={styles.pill}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-label="Select active states"
      >
        <span className={styles.pillGlyph}>{FLAG_GLYPH}</span>
        <span className={styles.pillLabel}>
          {activeStates.length === 0
            ? 'All'
            : activeStates.length <= 3
              ? activeStates.join(', ')
              : `${activeStates.length} states`}
        </span>
      </button>

      {open && (
        <div className={styles.sheet} role="dialog" aria-label="State selector">
          <input
            className={styles.search}
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Search states…"
            autoFocus
          />
          <div className={styles.grid}>
            {filtered.map(([code, name]) => {
              const selected = activeStates.includes(code);
              return (
                <button
                  key={code}
                  type="button"
                  className={`${styles.stateBtn} ${selected ? styles.stateBtnActive : ''}`}
                  onClick={() => toggle(code)}
                  aria-pressed={selected}
                  title={name}
                >
                  {code}
                </button>
              );
            })}
          </div>
          {activeStates.length > 0 && (
            <div className={styles.footer}>
              <span className={styles.footerCount}>{activeStates.length} selected</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
