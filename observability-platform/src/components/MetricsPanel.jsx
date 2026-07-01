export default function MetricsPanel({ selectedService, instance, metrics }) {
  const cpuData = metrics?.cpu;
  const cpuValue = cpuData?.avg || 0;

  return (
    <div className="panel" style={{ flex: '0 0 auto' }}>
      <div className="panel-header">
        <h2>Metrics</h2>
        {selectedService && (
          <span className="panel-count">{selectedService.type.toUpperCase()}</span>
        )}
      </div>
      {selectedService && instance ? (
        <div className="metrics-grid">
          <div className="metric-card">
            <div className="metric-label">CPU Utilization</div>
            <div className={`metric-value ${cpuValue > 80 ? 'red' : cpuValue > 60 ? 'orange' : 'green'}`}>
              {cpuData ? `${cpuValue.toFixed(1)}%` : '—'}
            </div>
            <svg className="metric-sparkline" viewBox="0 0 100 32" preserveAspectRatio="none">
              <polyline
                fill="none"
                stroke="var(--accent-cyan)"
                strokeWidth="1.5"
                points={[...Array(20)].map((_, i) => `${i * 5},${16 + Math.sin(Date.now() / 1000 + i) * 10}`).join(' ')}
              />
            </svg>
          </div>
          <div className="metric-card">
            <div className="metric-label">Time Slice</div>
            <div className="metric-value cyan">1h</div>
          </div>
        </div>
      ) : (
        <div style={{ textAlign: 'center', padding: '24px 0', color: 'var(--text-secondary)', fontSize: '13px' }}>
          Select a service to view metrics
        </div>
      )}
    </div>
  );
}
