import { useEffect, useRef, useState } from 'react';
import { useOracleState } from '../state';
import styles from './TabBar.module.css';

// Inline glyphs — stroke-based, inherit currentColor so the amber filament
// state colors them for free. No icon library (house rule: zero UI deps).
const GLYPHS = {
  marketplace: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20h13V9.5" />
      <path d="M9.5 20v-6h5v6" />
    </svg>
  ),
  house: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20h13V9.5" />
      <path d="M12 11.5v5M9.5 14h5" />
    </svg>
  ),
  clients: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="9" cy="8.5" r="3.2" />
      <path d="M3.5 19.5c0-3 2.5-5 5.5-5s5.5 2 5.5 5" />
      <circle cx="16.5" cy="9.5" r="2.4" />
      <path d="M16 14.7c2.6.3 4.5 2 4.5 4.3" />
    </svg>
  ),
  comms: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 5.5h16v11H9l-5 4v-15Z" />
      <path d="M8 9.5h8M8 12.5h5" />
    </svg>
  ),
  profile: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="10" r="3" />
      <path d="M6.5 19a6.5 6.5 0 0 1 11 0" />
    </svg>
  ),
  // Platform-admin OPS key — the live pulse of the whole fleet.
  ops: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 12h3.5l2.5-6 4 12 2.5-6H21" />
    </svg>
  ),
};

/**
 * The Deck — five etched-glass keys under a live amber filament.
 * The filament segment slides to the active key; when the real WS feed
 * (state.liveFeed) delivers a new event, the whole filament flashes once.
 */
export function TabBar({ tabs, active, onSelect }) {
  const state = useOracleState();
  const feedLen = state.liveFeed?.length ?? 0;

  // Flash the filament on genuine feed activity only — never simulated.
  const [flash, setFlash] = useState(false);
  const prevLen = useRef(feedLen);
  useEffect(() => {
    if (feedLen > prevLen.current) {
      setFlash(true);
      const t = setTimeout(() => setFlash(false), 900);
      prevLen.current = feedLen;
      return () => clearTimeout(t);
    }
    prevLen.current = feedLen;
  }, [feedLen]);

  const activeIndex = Math.max(0, tabs.findIndex((t) => t.id === active));

  return (
    <nav className={styles.deck} aria-label="Oracle CRM">
      <div className={`${styles.filamentTrack} ${flash ? styles.filamentFlash : ''}`}>
        <div
          className={styles.filament}
          style={{ transform: `translateX(${activeIndex * 100}%)`, width: `${100 / tabs.length}%` }}
        />
      </div>
      <div className={styles.keys} role="tablist">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={tab.id === active}
            aria-controls={`view-${tab.id}`}
            className={`${styles.key} ${tab.id === active ? styles.keyActive : ''}`}
            onClick={() => onSelect(tab.id)}
          >
            <span className={styles.glyph}>{GLYPHS[tab.glyph]}</span>
            <span className={styles.label}>{tab.label}</span>
          </button>
        ))}
      </div>
    </nav>
  );
}
