/**
 * Bounded, adaptive scheduling of live quality checks (prompt 12, T12.03,
 * CLAUDE.md rule 4: capture reliability over AI frame rate).
 *
 * The camera preview and the recording never wait for this. At most ONE frame
 * is with the worker at a time; a frame offered while one is in flight is
 * dropped (counted, never queued), so a slow phone sees fewer hints, never a
 * stalled preview or a growing backlog. The interval between samples adapts
 * to how long the worker actually took (measured, not assumed): four times the
 * last processing time, clamped to [MIN_INTERVAL_MS, MAX_INTERVAL_MS].
 */

export const MIN_INTERVAL_MS = 250;
export const MAX_INTERVAL_MS = 2000;
const COST_MULTIPLIER = 4;

export class HintScheduler {
  constructor({ now = () => Date.now() } = {}) {
    this.now = now;
    this.inFlight = false;
    this.startedAt = 0;
    this.lastSampleAt = -Infinity;
    this.intervalMs = MIN_INTERVAL_MS;
    this.lastCostMs = null;
    this.analysed = 0;
    this.dropped = 0;
  }

  /** Whether a frame should be sampled now. Dropping is counted. */
  shouldSample() {
    const t = this.now();
    if (t - this.lastSampleAt < this.intervalMs) return false;
    if (this.inFlight) {
      this.dropped += 1;
      return false;
    }
    return true;
  }

  /** A frame was handed to the worker. */
  started() {
    this.inFlight = true;
    this.startedAt = this.now();
    this.lastSampleAt = this.startedAt;
  }

  /** The worker answered (or failed): free the slot and adapt the interval. */
  finished() {
    if (!this.inFlight) return;
    this.inFlight = false;
    this.lastCostMs = Math.max(0, this.now() - this.startedAt);
    this.analysed += 1;
    this.intervalMs = Math.min(MAX_INTERVAL_MS, Math.max(MIN_INTERVAL_MS, this.lastCostMs * COST_MULTIPLIER));
  }

  stats() {
    return { analysed: this.analysed, dropped: this.dropped, intervalMs: this.intervalMs, lastCostMs: this.lastCostMs };
  }
}
