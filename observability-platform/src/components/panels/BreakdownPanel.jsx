// Generic horizontal-bar breakdown — reuses the BillingPanel .cost-bar CSS.
// buckets = [{ label, value, color? }]. unit: '' for counts, '$' for cost.
export default function BreakdownPanel({ title, buckets, unit = '', empty = 'no data' }) {
  const items = buckets || [];
  const max = items.reduce((m, b) => Math.max(m, b.value), 0) || 1;
  const fmt = (v) => (unit === '$' ? `$${(v || 0).toFixed(2)}` : `${v}${unit && unit !== '$' ? unit : ''}`);

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>{title}</h2>
        <span className="panel-count">{items.length}</span>
      </div>
      {items.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '18px', color: 'var(--text-secondary)', fontSize: '13px' }}>
          {empty}
        </div>
      ) : (
        <div className="service-cost-list">
          {items.map((b, i) => (
            <div key={`${b.label}-${i}`} className="service-cost-item">
              <span style={{ flex: '0 0 118px', fontSize: '11px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {b.label}
              </span>
              <div className="cost-bar">
                <div
                  className="cost-bar-fill"
                  style={{ width: `${(b.value / max) * 100}%`, background: b.color || 'var(--accent-cyan)' }}
                />
              </div>
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', color: b.color || 'var(--accent-cyan)', minWidth: '48px', textAlign: 'right' }}>
                {fmt(b.value)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
