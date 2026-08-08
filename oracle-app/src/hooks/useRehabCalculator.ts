/**
 * useRehabCalculator — real-time rehab line items driven by 3D editor geometry.
 *
 * Feeds the EXISTING underwriting path rather than becoming a second source of
 * truth: `underwritingPayload.rehab_items` is exactly the array
 * `calculate_underwriting` (backend/intelligence_engine.py) consumes, so the
 * 3D layout flows into the same ARV → rehab → MAO trace, and therefore into
 * the distress-valuation safety layer that caps offers at the 70% rule.
 *
 * The `subtotal`/`total` numbers here are a PREVIEW. The server recomputes in
 * Decimal and its answer wins. Never persist the browser total as the estimate.
 */

import { useCallback, useMemo, useState } from 'react';
import { DEFAULT_COST_LINES, quantityFor, roundQuantity } from '../lib/floorplan/rehabCostModel';
import type { CostLine } from '../lib/floorplan/rehabCostModel';
import { diffMetrics, toImperial } from '../lib/floorplan/metrics';
import type { ImperialMetrics } from '../lib/floorplan/metrics';
import type { SpatialMetrics } from '../lib/floorplan/protocol';

export interface RehabLineItem {
  key: string;
  label: string;
  category: string;
  unit: string;
  quantity: number;
  unitCost: number;
  /** Preview only — server recomputes. */
  subtotal: number;
  basis: string;
  enabled: boolean;
  /** Change in subtotal vs the baseline layout, for the live delta badge. */
  delta: number;
}

/** Shape accepted by POST /api/intelligence/underwriting. */
export interface UnderwritingRehabItem {
  category: string;
  quantity: number;
  unit_cost: number;
  basis: string;
}

export interface UseRehabCalculatorResult {
  lines: RehabLineItem[];
  imperial: ImperialMetrics;
  /** Preview total across enabled lines. */
  previewTotal: number;
  /** Preview total change vs baseline. */
  previewDelta: number;
  /** Total square footage — feeds `subject_sqft`. */
  totalSqft: number;
  /** Ready to POST to the underwriting endpoint. */
  underwritingPayload: {
    subject_sqft: number;
    rehab_items: UnderwritingRehabItem[];
  };
  toggleLine: (key: string, enabled: boolean) => void;
  setUnitCost: (key: string, unitCost: number) => void;
  /** Restore the shipped cost table. */
  resetOverrides: () => void;
  /** True when the agent has edited any unit cost. */
  hasOverrides: boolean;
}

export interface UseRehabCalculatorOptions {
  /** Live metrics from the editor bridge (metric units). */
  metrics: SpatialMetrics;
  /** Metrics of the last-saved layout — deltas are measured against this. */
  baselineMetrics?: SpatialMetrics;
  /** Override the cost table (per-market pricing). */
  costLines?: readonly CostLine[];
}

export function useRehabCalculator({
  metrics,
  baselineMetrics,
  costLines = DEFAULT_COST_LINES,
}: UseRehabCalculatorOptions): UseRehabCalculatorResult {
  const [disabled, setDisabled] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    for (const line of costLines) if (!line.enabledByDefault) initial.add(line.key);
    return initial;
  });
  const [overrides, setOverrides] = useState<Record<string, number>>({});

  const imperial = useMemo(() => toImperial(metrics), [metrics]);

  // Baseline imperial metrics, for the "+$4,200 since last save" badge.
  const baselineImperial = useMemo(
    () => (baselineMetrics ? toImperial(baselineMetrics) : null),
    [baselineMetrics],
  );

  const lines = useMemo<RehabLineItem[]>(() => {
    return costLines.map((line) => {
      const unitCost = overrides[line.key] ?? line.unitCost;
      const quantity = roundQuantity(line, quantityFor(line, imperial));
      const enabled = !disabled.has(line.key);
      const subtotal = enabled ? quantity * unitCost : 0;

      let delta = 0;
      if (baselineImperial && enabled) {
        const baseQty = roundQuantity(line, quantityFor(line, baselineImperial));
        delta = (quantity - baseQty) * unitCost;
      }

      return {
        key: line.key,
        label: line.label,
        category: line.category,
        unit: line.unit,
        quantity,
        unitCost,
        subtotal,
        basis: line.basis,
        enabled,
        delta,
      };
    });
  }, [costLines, imperial, baselineImperial, overrides, disabled]);

  const previewTotal = useMemo(
    () => lines.reduce((sum, l) => sum + l.subtotal, 0),
    [lines],
  );
  const previewDelta = useMemo(
    () => lines.reduce((sum, l) => sum + l.delta, 0),
    [lines],
  );

  const totalSqftValue = Math.round(imperial.floor_area_sqft);

  const underwritingPayload = useMemo(() => ({
    // The engine requires subject_sqft >= 1. An empty plan would 422; send 1
    // and let the caller gate on `totalSqft === 0` for a friendlier message.
    subject_sqft: Math.max(1, totalSqftValue),
    rehab_items: lines
      .filter((l) => l.enabled && l.quantity > 0)
      .map<UnderwritingRehabItem>((l) => ({
        category: l.category,
        quantity: l.quantity,
        unit_cost: l.unitCost,
        basis: l.basis,
      })),
  }), [lines, totalSqftValue]);

  const toggleLine = useCallback((key: string, enabled: boolean) => {
    setDisabled((prev) => {
      const next = new Set(prev);
      if (enabled) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const setUnitCost = useCallback((key: string, unitCost: number) => {
    setOverrides((prev) => ({
      ...prev,
      [key]: Number.isFinite(unitCost) && unitCost >= 0 ? unitCost : 0,
    }));
  }, []);

  const resetOverrides = useCallback(() => setOverrides({}), []);

  return {
    lines,
    imperial,
    previewTotal,
    previewDelta,
    totalSqft: totalSqftValue,
    underwritingPayload,
    toggleLine,
    setUnitCost,
    resetOverrides,
    hasOverrides: Object.keys(overrides).length > 0,
  };
}

/** Re-exported so callers can log what the last edit touched. */
export { diffMetrics };
