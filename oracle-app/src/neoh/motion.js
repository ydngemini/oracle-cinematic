import { useReducedMotion } from 'framer-motion';
import { useMemo } from 'react';

import { hasHighMotionBudget } from '../components/motion/AdaptiveViewTransition';

/**
 * motion — one spring, one policy.
 *
 * The Neoh surface is a single object that changes shape: pill, bar, panel.
 * One spring for every shape change is what makes it read as the same thing
 * moving rather than one thing replaced by another. Reduced motion means
 * instant; a low motion budget means no layout animation at all (the states
 * still render, they just cut).
 */
export const SPRING = Object.freeze({ type: 'spring', stiffness: 420, damping: 38, mass: 0.9 });
export const INSTANT = Object.freeze({ duration: 0 });

export function useMotionPolicy() {
  const reduced = useReducedMotion();
  return useMemo(() => {
    const budget = typeof window === 'undefined' ? true : hasHighMotionBudget();
    return {
      reduced: Boolean(reduced),
      layout: !reduced && budget,
      transition: reduced ? INSTANT : SPRING,
    };
  }, [reduced]);
}
