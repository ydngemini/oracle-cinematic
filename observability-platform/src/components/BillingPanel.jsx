import { useMemo } from 'react';

export default function BillingPanel({ billing }) {
  const { month_total, by_service } = billing;
  const sortedServices = useMemo(() => {
    if (!by_service) return [];
    return Object.entries(by_service)
      .sort((a, b) => b[1].total - a[1].total)
      .slice(0, 8);
  }, [by_service]);

  const maxCost = sortedServices[0]?.[1].total || 1;

  return (
    <div className="panel" style={{ flex: '1' }}>
      <div className="panel-header">
        <h2>Billing</h2>
        <span className="panel-count">30d</span>
      </div>
      <div className="billing-total">
        <div className="label">Current Month</div>
        <div className="amount">${month_total?.toFixed(2) || '0.00'}</div>
      </div>
      <div className="service-cost-list">
        {sortedServices.map(([service, data]) => (
          <div key={service} className="service-cost-item">
            <span style={{ flex: '0 0 120px', fontSize: '11px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {service.replace('Amazon ', '').replace('AWS ', '')}
            </span>
            <div className="cost-bar">
              <div 
                className="cost-bar-fill" 
                style={{ width: `${(data.total / maxCost) * 100}%` }} 
              />
            </div>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--accent-purple)' }}>
              ${data.total.toFixed(2)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
