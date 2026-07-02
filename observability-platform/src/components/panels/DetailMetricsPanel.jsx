import { useState, useEffect } from 'react';
import { fetchResourceMetrics } from '../../lib/api';

// Last-value CloudWatch value out of a datapoint {avg,max,min,sum} (or null).
const dp = (d) => (d == null ? null : d.avg ?? d.sum ?? d.max ?? null);
const pct = (v) => (v == null ? '—' : `${v.toFixed(1)}%`);
const num = (v) => (v == null ? '—' : Math.round(v).toLocaleString());
const ms = (v) => (v == null ? '—' : `${Math.round(v)} ms`);
const bytes = (v) => {
  if (v == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let n = v;
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
};

const FIELDS = {
  ec2: [
    { key: 'cpu', label: 'CPU', fmt: pct },
    { key: 'network_in', label: 'Net In', fmt: bytes },
    { key: 'network_out', label: 'Net Out', fmt: bytes },
    { key: 'disk_read_bytes', label: 'Disk Read', fmt: bytes },
    { key: 'disk_write_bytes', label: 'Disk Write', fmt: bytes },
    { key: 'status_check_failed', label: 'Status Chk', fmt: num },
  ],
  rds: [
    { key: 'cpu', label: 'CPU', fmt: pct },
    { key: 'connections', label: 'Connections', fmt: num },
    { key: 'freeable_memory', label: 'Free Mem', fmt: bytes },
    { key: 'free_storage', label: 'Free Storage', fmt: bytes },
    { key: 'read_iops', label: 'Read IOPS', fmt: num },
    { key: 'write_iops', label: 'Write IOPS', fmt: num },
  ],
  lambda: [
    { key: 'errors', label: 'Errors', fmt: num },
    { key: 'throttles', label: 'Throttles', fmt: num },
    { key: 'duration_avg', label: 'Duration', fmt: ms },
    { key: 'concurrent', label: 'Concurrent', fmt: num },
  ],
};

export default function DetailMetricsPanel({ selectedService }) {
  const type = selectedService?.type;
  const id = selectedService?.id;
  const supported = type && FIELDS[type];
  const [state, setState] = useState({ loading: false, error: '', data: null });

  useEffect(() => {
    if (!supported || !id) { setState({ loading: false, error: '', data: null }); return; }
    let cancelled = false;
    setState((s) => ({ ...s, loading: true, error: '' }));
    fetchResourceMetrics(type, id)
      .then((d) => { if (!cancelled) setState({ loading: false, error: '', data: d }); })
      .catch(() => { if (!cancelled) setState({ loading: false, error: 'metrics unavailable', data: null }); });
    return () => { cancelled = true; };
  }, [type, id, supported]);

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Resource Metrics</h2>
        {supported && <span className="panel-count">{type.toUpperCase()}</span>}
      </div>
      {!supported ? (
        <div style={{ textAlign: 'center', padding: '20px 0', color: 'var(--text-secondary)', fontSize: '13px' }}>
          Select an EC2, RDS, or Lambda resource
        </div>
      ) : state.loading ? (
        <div style={{ textAlign: 'center', padding: '20px 0', color: 'var(--text-secondary)', fontSize: '13px' }}>loading…</div>
      ) : state.error ? (
        <div style={{ textAlign: 'center', padding: '20px 0', color: 'var(--accent-orange)', fontSize: '13px' }}>{state.error}</div>
      ) : (
        <>
          <div className="service-meta" style={{ marginBottom: '8px' }}>{id}</div>
          <div className="metrics-grid" style={{ gridTemplateColumns: 'repeat(2, 1fr)' }}>
            {FIELDS[type].map((f) => (
              <div key={f.key} className="metric-card">
                <div className="metric-label">{f.label}</div>
                <div className="metric-value cyan">{f.fmt(dp(state.data?.[f.key]))}</div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
