import { useRef } from 'react';
import styles from './TabBar.module.css';

/**
 * Three destinations at the thumb line.
 *
 * This was an instrument rail: a live amber filament slid to the active key
 * and the whole track flashed on every WebSocket event. It was the most
 * animated thing on screen, permanently, underneath whatever the person was
 * actually reading — and a feed arriving is not news the navigation should
 * deliver. Active state is now full-strength ink and one small dot.
 */
export function TabBar({ tabs, active, onSelect }) {
  const keyRefs = useRef(new Map());

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
