import { useMemo } from 'react';
import { IndicatorLight } from '../RetroGauges.jsx';
import styles from './OperationalOverlays.module.css';

const defaultMetrics = {
  revenue: { current: 124580, target: 150000, trend: 8.5 },
  customers: { active: 842, new: 23, churned: 5 },
  transactions: { count: 15834, volume: 892450, avgValue: 56.34 },
  nps: { score: 72, trend: 3 },
};

export default function OperationalOverlays({ 
  metrics = defaultMetrics, 
  services,
  showRevenue = true,
  showCustomers = true,
  showHealth = true
}) {
  const revenuePercent = useMemo(() => {
    if (!metrics?.revenue) return 0;
    return Math.min(100, (metrics.revenue.current / metrics.revenue.target) * 100);
  }, [metrics]);

  const customerGrowth = useMemo(() => {
    if (!metrics?.customers) return 0;
    return metrics.customers.new - metrics.customers.churned;
  }, [metrics]);

  const systemHealth = useMemo(() => {
    if (!services) return 95;
    const total = services.length;
    if (total === 0) return 95;
    const healthy = services.filter(s => 
      s.state === 'running' || s.status === 'available'
    ).length;
    return Math.round((healthy / total) * 100);
  }, [services]);

  const formatCurrency = (value) => {
    if (value >= 1000000) return `$${(value / 1000000).toFixed(1)}M`;
    if (value >= 1000) return `$${(value / 1000).toFixed(1)}K`;
    return `$${value.toFixed(0)}`;
  };

  return (
    <div className={styles.operationalOverlay}>
      <div className={styles.overlayHeader}>
        <h3>BUSINESS METRICS</h3>
        <div className={styles.healthIndicator}>
          <IndicatorLight 
            status={systemHealth >= 95 ? 'ok' : systemHealth >= 80 ? 'warning' : 'critical'} 
            label={`${systemHealth}% HEALTH`} 
          />
        </div>
      </div>

      {showRevenue && (
        <div className={styles.metricSection}>
          <div className={styles.sectionTitle}>REVENUE</div>
          <div className={styles.revenueCard}>
            <div className={styles.revenuePrimary}>
              <span className={styles.revenueValue}>
                {formatCurrency(metrics?.revenue?.current || 0)}
              </span>
              <span className={styles.revenueLabel}>MTD Revenue</span>
            </div>
            <div className={styles.revenueTarget}>
              <div className={styles.targetBar}>
                <div 
                  className={styles.targetFill}
                  style={{ width: `${revenuePercent}%` }}
                />
              </div>
              <div className={styles.targetLabels}>
                <span>{revenuePercent.toFixed(0)}%</span>
                <span>{formatCurrency(metrics?.revenue?.target || 0)} target</span>
              </div>
            </div>
            <div className={`${styles.trendChip} ${metrics?.revenue?.trend >= 0 ? styles.positive : styles.negative}`}>
              {metrics?.revenue?.trend >= 0 ? '↑' : '↓'} {Math.abs(metrics?.revenue?.trend || 0)}%
            </div>
          </div>
        </div>
      )}

      {showCustomers && (
        <div className={styles.metricSection}>
          <div className={styles.sectionTitle}>CUSTOMERS</div>
          <div className={styles.customerGrid}>
            <div className={styles.customerCard}>
              <span className={styles.customerValue}>{metrics?.customers?.active || 0}</span>
              <span className={styles.customerLabel}>Active</span>
            </div>
            <div className={`${styles.customerCard} ${styles.newCard}`}>
              <span className={styles.customerValue}>+{metrics?.customers?.new || 0}</span>
              <span className={styles.customerLabel}>New</span>
            </div>
            <div className={`${styles.customerCard} ${styles.churnCard}`}>
              <span className={styles.customerValue}>-{metrics?.customers?.churned || 0}</span>
              <span className={styles.customerLabel}>Churn</span>
            </div>
            <div className={`${styles.customerCard} ${customerGrowth >= 0 ? styles.positive : styles.negative}`}>
              <span className={styles.customerValue}>
                {customerGrowth >= 0 ? '+' : ''}{customerGrowth}
              </span>
              <span className={styles.customerLabel}>Net</span>
            </div>
          </div>
        </div>
      )}

      <div className={styles.metricSection}>
        <div className={styles.sectionTitle}>TRANSACTIONS</div>
        <div className={styles.transactionRow}>
          <div className={styles.transactionMetric}>
            <span className={styles.transValue}>
              {(metrics?.transactions?.count || 0).toLocaleString()}
            </span>
            <span className={styles.transLabel}>Count</span>
          </div>
          <div className={styles.transactionMetric}>
            <span className={styles.transValue}>
              {formatCurrency(metrics?.transactions?.volume || 0)}
            </span>
            <span className={styles.transLabel}>Volume</span>
          </div>
          <div className={styles.transactionMetric}>
            <span className={styles.transValue}>
              ${metrics?.transactions?.avgValue?.toFixed(2) || '0.00'}
            </span>
            <span className={styles.transLabel}>Avg Value</span>
          </div>
        </div>
      </div>

      <div className={styles.npsSection}>
        <div className={styles.npsLabel}>NPS SCORE</div>
        <div className={styles.npsDisplay}>
          <span className={styles.npsValue}>{metrics?.nps?.score || 0}</span>
          <span className={`${styles.npsTrend} ${metrics?.nps?.trend >= 0 ? styles.positive : styles.negative}`}>
            {metrics?.nps?.trend >= 0 ? '↑' : '↓'} {Math.abs(metrics?.nps?.trend || 0)}
          </span>
        </div>
      </div>
    </div>
  );
}

export function MiniMetricWidget({ value, label, trend, type = 'default' }) {
  return (
    <div className={`${styles.miniWidget} ${styles[type]}`}>
      <span className={styles.miniValue}>{value}</span>
      <span className={styles.miniLabel}>{label}</span>
      {trend !== undefined && (
        <span className={`${styles.miniTrend} ${trend >= 0 ? styles.positive : styles.negative}`}>
          {trend >= 0 ? '↑' : '↓'} {Math.abs(trend)}
        </span>
      )}
    </div>
  );
}
