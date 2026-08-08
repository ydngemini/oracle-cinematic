import * as React from 'react';

const { Fragment, useState } = React;

// Shared feature detection intentionally lives beside the transition wrapper.
// eslint-disable-next-line react-refresh/only-export-components
export function hasHighMotionBudget() {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return false;
  if (navigator.connection?.saveData === true) return false;

  const logicalCores = navigator.hardwareConcurrency || 8;
  const memoryGb = navigator.deviceMemory || 8;
  return logicalCores > 4 && memoryGb > 4;
}

/**
 * Native view transitions capture compositor snapshots. Keep them on devices
 * with enough parallel decode/raster capacity; low-power and data-saving
 * devices render the same state change directly.
 */
export function AdaptiveViewTransition({ children, ...transitionProps }) {
  const [enabled] = useState(hasHighMotionBudget);
  const ViewTransition = React.ViewTransition;

  if (!enabled || typeof ViewTransition !== 'function') {
    return <Fragment>{children}</Fragment>;
  }
  return <ViewTransition {...transitionProps}>{children}</ViewTransition>;
}
