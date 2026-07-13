import { useState, useEffect } from 'react';
import { fetchMetricHistory } from '../lib/api';
import TimeSeriesChart from './TimeSeriesChart.jsx';

const RANGES = ['1h', '6h', '24h', '7d'];

export default function MetricsPanel({ selectedService, instance }) {
  const type = selectedService?.type;
  const id = selectedService?.id;
  const [range, setRange] = useState('1h');
  const [hist, setHist] = useState({ loading: false, error: '', series: [], unit: '', metric: '' });

  // Fetch the real CloudWatch history whenever the selected resource or range changes.
  useEffect(() => {
    if (!type || !id) {
      setHist({ loading: false, error: '', series: [], unit: '', metric: '' });
      return;
    }
    let cancelled = false;
    setHist((h) => ({ ...h, loading: true, error: '' }));
    fetchMetricHistory(type, id, range)
      .then((d) => {
        if (!cancelled) setHist({ loading: false, error: '', series: d.series || [], unit: d.unit || '', metric: d.metric || '' });
      })
      .catch(() => {
        if (!cancelled) setHist({ loading: false, error: 'history unavailable', series: [], unit: '', metric: '' });
      });
    return () => { cancelled = true; };
  }, [type, id, range]);

  const isPct = hist.unit === 'Percent';
  const last = hist.series.length ? hist.series[hist.series.length - 1].v : null;
  const valClass = isPct ? (last > 80 ? 'red' : last > 60 ? 'orange' : 'green') : 'cyan';
  const label = hist.metric || (type === 'lambda' ? 'Invocations' : 'CPU Utilization');

  return (
    <div className="panel" style={{ flex: '0 0 auto' }}>
      <div className="panel-header">
        <h2>Metrics</h2>
        {selectedService && <span className="panel-count">{selectedService.type.toUpperCase()}</span>}
      </div>
      {selectedService && instance ? (
        <>
          <div className="metrics-grid">
            <div className="metric-card">
              <div className="metric-label">{label}</div>
              <div className={`metric-value ${valClass}`}>
                {last != null ? `${last.toFixed(1)}${isPct ? '%' : ''}` : '—'}
              </div>
            </div>
            <div className="metric-card">
              <div className="metric-label">Range</div>
              <div className="range-selector">
                {RANGES.map((r) => (
                  <button
                    type="button"
                    key={r}
                    className={`range-btn ${r === range ? 'active' : ''}`}
                    onClick={() => setRange(r)}
                  >
                    {r}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <TimeSeriesChart
            series={hist.series}
            unit={hist.unit}
            loading={hist.loading}
            error={hist.error}
          />
        </>
      ) : (
        <div style={{ textAlign: 'center', padding: '24px 0', color: 'var(--text-secondary)', fontSize: '13px' }}>
          Select a service to view metrics
        </div>
      )}
    </div>
  );
}
