import { useEffect } from 'react';

const FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])', 'textarea:not([disabled])', '[tabindex]:not([tabindex="-1"])',
].join(',');

function focusables(root) {
  return Array.from(root?.querySelectorAll(FOCUSABLE) || [])
    .filter((el) => !el.closest('[inert]') && el.getAttribute('aria-hidden') !== 'true');
}

/**
 * Keyboard contract for a modal sheet: focus moves in when it opens, Tab and
 * Shift+Tab cycle inside it, and focus returns to whatever opened it when it
 * closes. Escape stays with the caller (some sheets refuse it while saving).
 *
 * The quick-add client sheet is aria-modal but had none of this — Tab walked
 * out of the dialog onto the tab bar and the Neoh pill behind the scrim.
 */
export function useDialogFocus(ref, open) {
  useEffect(() => {
    if (!open) return undefined;
    const opener = document.activeElement;
    const frame = window.requestAnimationFrame(() => {
      const root = ref.current;
      if (root && !root.contains(document.activeElement)) (focusables(root)[0] || root).focus?.({ preventScroll: true });
    });
    const onKey = (event) => {
      if (event.key !== 'Tab') return;
      const root = ref.current;
      if (!root) return;
      const items = focusables(root);
      if (!items.length) { event.preventDefault(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      const inside = root.contains(document.activeElement);
      if (event.shiftKey && (!inside || document.activeElement === first)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (!inside || document.activeElement === last)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKey, true);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener('keydown', onKey, true);
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus({ preventScroll: true });
    };
  }, [open, ref]);
}

export default useDialogFocus;
