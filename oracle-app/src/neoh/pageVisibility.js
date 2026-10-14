import { useEffect, useRef, useState } from 'react';

/**
 * True while the tab is hidden, so nothing animates off-screen. Shared by the
 * flat mascot and the 3D corner so both hold still at the same moment.
 */
export function usePageHidden() {
  const [hidden, setHidden] = useState(() => (typeof document === 'undefined' ? false : document.hidden));
  const frame = useRef(0);
  useEffect(() => {
    if (typeof document === 'undefined') return undefined;
    const onChange = () => {
      window.cancelAnimationFrame(frame.current);
      frame.current = window.requestAnimationFrame(() => setHidden(document.hidden));
    };
    document.addEventListener('visibilitychange', onChange);
    return () => {
      window.cancelAnimationFrame(frame.current);
      document.removeEventListener('visibilitychange', onChange);
    };
  }, []);
  return hidden;
}
