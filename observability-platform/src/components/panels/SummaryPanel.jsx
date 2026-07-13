// Generic summary panel — a grid of metric cards. Reuses .metrics-grid /
// .metric-card / .metric-value CSS. cards = [{ label, value, className }].
export default function SummaryPanel({ title, count, cards, columns = 2 }) {
  return (
    <div className="panel">
      <div className="panel-header">
        <h2>{title}</h2>
        {count != null && <span className="panel-count">{count}</span>}
      </div>
      <div className="metrics-grid" style={{ gridTemplateColumns: `repeat(${columns}, 1fr)` }}>
        {(cards || []).map((c, i) => (
          <div key={i} className="metric-card">
            <div className="metric-label">{c.label}</div>
            <div className={`metric-value ${c.className || 'cyan'}`}>{c.value}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
