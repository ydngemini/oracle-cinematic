import { useEffect } from 'react';

/**
 * The first global shortcuts in the app: ⌘K (or Ctrl+K) and a bare "/" put
 * the cursor in Neoh; Escape hands control back. "/" only counts when the
 * person is not already typing somewhere — a slash in a note is a slash.
 */

export function isEditable(target) {
  if (!target || typeof target !== 'object') return false;
  const tag = (target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
  return Boolean(target.isContentEditable);
}

/** Pure: which action, if any, a keydown maps to. */
export function shortcutFor(event) {
  if (!event) return null;
  const meta = event.metaKey || event.ctrlKey;
  if (meta && (event.key === 'k' || event.key === 'K')) return 'focus';
  if (event.key === 'Escape') return 'escape';
  if (event.key === '/' && !meta && !event.altKey && !isEditable(event.target)) return 'focus';
  return null;
}

export function useGlobalShortcuts({ onFocus, onEscape, enabled = true }) {
  useEffect(() => {
    if (!enabled) return undefined;
    const onKeyDown = (event) => {
      const action = shortcutFor(event);
      if (action === 'focus') {
        event.preventDefault();
        onFocus?.();
      } else if (action === 'escape') {
        // Not preventDefault: dialogs beneath still see their own Escape.
        onEscape?.(event);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [enabled, onEscape, onFocus]);
}
