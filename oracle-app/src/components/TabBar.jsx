import { useEffect, useRef, useState } from 'react';
import { useOracleState } from '../state';
import styles from './TabBar.module.css';

/**
 * The Deck — compact etched-glass keys under a live amber filament.
 * The filament segment slides to the active key; when the real WS feed
 * (state.liveFeed) delivers a new event, the whole filament flashes once.
 */
export function TabBar({ tabs, active, onSelect }) {
  const state = useOracleState();
  const feedLen = state.liveFeed?.length ?? 0;

  // Flash the filament on genuine feed activity only — never simulated.
  const [flash, setFlash] = useState(false);
  const prevLen = useRef(feedLen);
  const keyRefs = useRef(new Map());
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

  const moveFocus = (index) => {
    const normalized = (index + tabs.length) % tabs.length;
    const tab = tabs[normalized];
    keyRefs.current.get(tab.id)?.focus();
  };

  const onKeyDown = (event, index) => {
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      moveFocus(index + 1);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      moveFocus(index - 1);
    } else if (event.key === 'Home') {
      event.preventDefault();
      moveFocus(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      moveFocus(tabs.length - 1);
    }
  };

  return (
    <nav
      className={styles.deck}
      aria-label="Neoh CRM"
      style={{ viewTransitionName: 'crm-deck' }}
    >
      <div className={`${styles.filamentTrack} ${flash ? styles.filamentFlash : ''}`}>
        <div
          className={styles.filament}
          style={{ transform: `translateX(${activeIndex * 100}%)`, width: `${100 / tabs.length}%` }}
        />
      </div>
      <div
        className={styles.keys}
        role="tablist"
        style={{ gridTemplateColumns: `repeat(${tabs.length}, 1fr)` }}
      >
        {tabs.map((tab, index) => {
          const Icon = tab.Icon;
          return (
          <button
            key={tab.id}
            ref={(node) => {
              if (node) {
                keyRefs.current.set(tab.id, node);
              } else {
                keyRefs.current.delete(tab.id);
              }
            }}
            id={`tab-${tab.id}`}
            role="tab"
            aria-selected={tab.id === active}
            aria-controls={`view-${tab.id}`}
            aria-label={tab.label}
            tabIndex={tab.id === active ? 0 : -1}
            className={`${styles.key} ${tab.id === active ? styles.keyActive : ''}`}
            onClick={() => onSelect(tab.id)}
            onKeyDown={(event) => onKeyDown(event, index)}
            onPointerEnter={() => { void tab.preload?.(); }}
            onPointerDown={() => { void tab.preload?.(); }}
            onFocus={() => { void tab.preload?.(); }}
          >
            <span className={styles.glyph}>
              <Icon aria-hidden="true" strokeWidth={1.7} />
            </span>
            <span className={styles.label}>{tab.shortLabel || tab.label}</span>
          </button>
          );
        })}
      </div>
    </nav>
  );
}
