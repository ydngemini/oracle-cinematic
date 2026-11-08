/**
 * Off-main-thread live quality hints for Capture Studio. One message in, one
 * out: {id, width, height, buffer} -> {id, hints, costMs}. Keeps only the
 * previous frame's thumbnail (for the duplicate test); nothing is stored or
 * sent anywhere.
 */
import { frameHints, frameStats } from './frameQuality';

let previous = null;

self.onmessage = (event) => {
  const { id, width, height, buffer, reset } = event.data || {};
  if (reset) {
    previous = null;
    return;
  }
  const started = performance.now();
  try {
    const stats = frameStats({ width, height, data: new Uint8ClampedArray(buffer) });
    const hints = frameHints(stats, previous);
    previous = stats;
    self.postMessage({ id, hints, costMs: performance.now() - started });
  } catch {
    self.postMessage({ id, hints: null, costMs: performance.now() - started });
  }
};
