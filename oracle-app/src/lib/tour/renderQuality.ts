/**
 * Frame-rate governor: keep navigation smooth rather than maximally sharp.
 *
 * Starts at the level the device assessment chose and steps DOWN when the
 * measured frame rate stays low, so a phone that heats up mid-tour sheds pixels
 * instead of stuttering. It steps back up only to where it started, and only
 * after a long healthy stretch — oscillating between levels is worse than
 * either one.
 *
 * Pure and time-driven by the caller's `dt`, so it is testable without a
 * renderer and costs one addition per frame.
 */

import { PIXEL_RATIO, type QualityLevel } from './deviceCapability';

const ORDER: QualityLevel[] = ['performance', 'balanced', 'high'];

export interface GovernorOptions {
  /** Below this average fps for `downgradeAfterS`, drop one level. */
  lowFps?: number;
  /** Above this average fps for `upgradeAfterS`, raise one level (to ceiling). */
  highFps?: number;
  downgradeAfterS?: number;
  upgradeAfterS?: number;
}

export interface FrameRateGovernor {
  /** Feed one frame's dt in seconds. Returns the new level when it changes. */
  sample(dtSeconds: number): QualityLevel | null;
  level(): QualityLevel;
  /** Average fps over the current window — for diagnostics, not display. */
  averageFps(): number;
}

export function createFrameRateGovernor(initial: QualityLevel, opts: GovernorOptions = {}): FrameRateGovernor {
  const lowFps = opts.lowFps ?? 24;
  const highFps = opts.highFps ?? 55;
  const downAfter = opts.downgradeAfterS ?? 2.5;
  const upAfter = opts.upgradeAfterS ?? 12;
  const ceiling = ORDER.indexOf(initial);
  let index = ceiling;
  let windowTime = 0;
  let windowFrames = 0;
  let lowFor = 0;
  let highFor = 0;
  let lastAvg = 60;

  return {
    sample(dt: number) {
      // Ignore pauses (tab hidden, debugger, first frame after a load).
      if (!(dt > 0) || dt > 0.5) return null;
      windowTime += dt;
      windowFrames += 1;
      if (windowTime < 0.5) return null;
      const avg = windowFrames / windowTime;
      lastAvg = avg;
      const span = windowTime;
      windowTime = 0;
      windowFrames = 0;
      if (avg < lowFps) { lowFor += span; highFor = 0; } else if (avg > highFps) { highFor += span; lowFor = 0; } else { lowFor = 0; highFor = 0; }
      if (lowFor >= downAfter && index > 0) {
        index -= 1;
        lowFor = 0;
        return ORDER[index];
      }
      if (highFor >= upAfter && index < ceiling) {
        index += 1;
        highFor = 0;
        return ORDER[index];
      }
      return null;
    },
    level: () => ORDER[index],
    averageFps: () => lastAvg,
  };
}

export function pixelRatioFor(level: QualityLevel, devicePixelRatio: number): number {
  return Math.min(devicePixelRatio || 1, PIXEL_RATIO[level]);
}
