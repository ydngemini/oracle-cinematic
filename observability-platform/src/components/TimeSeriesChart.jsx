import { useMemo } from 'react';

// Dependency-free SVG time-series chart — matches the dashboard's raw-SVG/WebGL
// ethos (no charting lib). Renders a normalized area+line from [{t, v}] points
// with min / now / max labels. Handles loading / error / empty states.
export default function TimeSeriesChart({
  series = [],
  unit = '',
  color = 'var(--accent-cyan)',
  height = 96,
  loading = false,
  error = '',
}) {
  const { line, area, min, max, last } = useMemo(() => {
    const pts = (series || []).filter((p) => p && p.v != null);
    if (pts.length < 2) return { line: '', area: '', min: null, max: null, last: null };
    const vs = pts.map((p) => p.v);
    const lo = Math.min(...vs);
    const hi = Math.max(...vs);
    const span = hi - lo || 1;
    const W = 100;
    const H = 100;
    const pad = 8;
    const xy = pts.map((p, i) => {
      const x = (i / (pts.length - 1)) * W;
      const y = H - pad - ((p.v - lo) / span) * (H - 2 * pad);
      return [x, y];
    });
    const l = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`).join(' ');
    return { line: l, area: `${l} L100,100 L0,100 Z`, min: lo, max: hi, last: vs[vs.length - 1] };
  }, [series]);

  if (loading) return <div className="ts-chart ts-msg" style={{ height }}>loading…</div>;
  if (error) return <div className="ts-chart ts-msg ts-err" style={{ height }}>{error}</div>;
  if (!line) return <div className="ts-chart ts-msg" style={{ height }}>no data in range</div>;

  const suffix = unit === 'Percent' ? '%' : '';
  const fmt = (v) => (v == null ? '—' : Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : v.toFixed(1));

  return (
    <div className="ts-chart" style={{ height }}>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="ts-svg">
        <path d={area} fill={color} fillOpacity="0.14" />
        <path d={line} fill="none" stroke={color} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="ts-labels">
        <span>max {fmt(max)}{suffix}</span>
        <span className="ts-now">now {fmt(last)}{suffix}</span>
        <span>min {fmt(min)}{suffix}</span>
      </div>
    </div>
  );
}
