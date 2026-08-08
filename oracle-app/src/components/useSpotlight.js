import { useEffect, useState } from 'react';

/**
 * Resolve a CSS selector to a live, tracked bounding box for the guided
 * walkthrough spotlight.
 *
 * The hard part is timing, not geometry. Tour steps navigate to lazily-loaded
 * routes and tabs, so the element a step points at usually does not exist yet
 * when the step becomes active. This polls on animation frames until it
 * appears (bounded by `timeout`), then keeps the rect current against scroll,
 * resize, and layout shifts from late-arriving content.
 *
 * Returns null while unresolved — callers render an un-cut dim layer, so the
 * walkthrough degrades to its previous behaviour rather than pointing at the
 * wrong thing.
 */
export function useSpotlight(selector, { active = true, padding = 8, timeout = 4000 } = {}) {
  const [rect, setRect] = useState(null);

  useEffect(() => {
    let frame = 0;

    if (!active || !selector) {
      // Clear on a frame rather than synchronously: a setState in the effect
      // body triggers a cascading render (react-hooks/set-state-in-effect).
      frame = requestAnimationFrame(() => setRect(null));
      return () => cancelAnimationFrame(frame);
    }

    let cancelled = false;
    let element = null;
    const startedAt = performance.now();

    const measure = () => {
      if (!element) return;
      const box = element.getBoundingClientRect();
      // A zero-size box means the element is present but not laid out yet
      // (display:none, or inside a collapsed lazy boundary). Keep waiting.
      if (box.width === 0 && box.height === 0) {
        element = null;
        return;
      }
      setRect({
        top: box.top - padding,
        left: box.left - padding,
        width: box.width + padding * 2,
        height: box.height + padding * 2,
      });
    };

    const tick = () => {
      if (cancelled) return;

      if (!element) {
        element = document.querySelector(selector);
        if (element) {
          // Bring it into view before the first measure, so the spotlight
          // never lands on something off-screen.
          element.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
        } else if (performance.now() - startedAt > timeout) {
          setRect(null);
          return; // give up quietly; the panel still renders
        }
      }

      measure();
      frame = requestAnimationFrame(tick);
    };

    frame = requestAnimationFrame(tick);

    // Scroll/resize are handled by the rAF loop above, but listening as well
    // makes the box track immediately on fast scroll rather than a frame late.
    const onViewportChange = () => measure();
    window.addEventListener('scroll', onViewportChange, true);
    window.addEventListener('resize', onViewportChange);

    return () => {
      cancelled = true;
      cancelAnimationFrame(frame);
      window.removeEventListener('scroll', onViewportChange, true);
      window.removeEventListener('resize', onViewportChange);
    };
  }, [selector, active, padding, timeout]);

  return rect;
}

/**
 * Choose where to put the tour panel so it never covers the thing it is
 * pointing at. Prefers the side with the most room; falls back to the docked
 * bottom-right position when there is no anchor.
 */
export function placementFor(rect, panel = { width: 380, height: 300 }, gap = 16) {
  if (!rect) return { position: 'docked' };

  const vw = window.innerWidth;
  const vh = window.innerHeight;

  const room = {
    right: vw - (rect.left + rect.width),
    left: rect.left,
    bottom: vh - (rect.top + rect.height),
    top: rect.top,
  };

  // Horizontal first: side-by-side reads better than above/below for nav rails.
  if (room.right >= panel.width + gap) {
    return {
      position: 'anchored',
      left: Math.min(rect.left + rect.width + gap, vw - panel.width - gap),
      top: clamp(rect.top + rect.height / 2 - panel.height / 2, gap, vh - panel.height - gap),
      arrow: 'left',
    };
  }
  if (room.left >= panel.width + gap) {
    return {
      position: 'anchored',
      left: Math.max(rect.left - panel.width - gap, gap),
      top: clamp(rect.top + rect.height / 2 - panel.height / 2, gap, vh - panel.height - gap),
      arrow: 'right',
    };
  }
  if (room.bottom >= panel.height + gap) {
    return {
      position: 'anchored',
      left: clamp(rect.left + rect.width / 2 - panel.width / 2, gap, vw - panel.width - gap),
      top: rect.top + rect.height + gap,
      arrow: 'top',
    };
  }
  if (room.top >= panel.height + gap) {
    return {
      position: 'anchored',
      left: clamp(rect.left + rect.width / 2 - panel.width / 2, gap, vw - panel.width - gap),
      top: Math.max(rect.top - panel.height - gap, gap),
      arrow: 'bottom',
    };
  }

  // Nothing fits beside it — dock and let the cutout carry the emphasis.
  return { position: 'docked' };
}

function clamp(value, lo, hi) {
  return Math.max(lo, Math.min(hi, value));
}
