import { useMemo } from 'react';
import styles from './CostIntelligencePanel.module.css';

const defaultForecast = [
  { day: 'Mon', actual: 12.40, projected: null },
  { day: 'Tue', actual: 15.80, projected: null },
  { day: 'Wed', actual: 11.20, projected: null },
  { day: 'Thu', actual: 18.50, projected: null },
  { day: 'Fri', actual: 14.30, projected: null },
  { day: 'Sat', actual: 9.20, projected: null },
  { day: 'Sun', actual: null, projected: 8.50 },
];

const defaultRecommendations = [
  { 
    id: 1, 
    title: 'Right-size EC2 instances', 
    savings: 45.20, 
    type: 'compute',
    description: '3 instances underutilized (<10% CPU avg)' 
  },
  { 
    id: 2, 
    title: 'Reserved Instance purchase', 
    savings: 120.00, 
    type: 'commitment',
    description: 'Stable workload qualifies for 1-year RI' 
  },
  { 
    id: 3, 
    title: 'Delete unattached EBS volumes', 
    savings: 18.50, 
    type: 'storage',
    description: '2 volumes unattached for 30+ days' 
  },
];

export default function CostIntelligencePanel({ billing, forecast = defaultForecast, recommendations = defaultRecommendations }) {
  const monthlyTotal = billing?.month_total || 154.80;
  const weeklyTrend = useMemo(() => {
    if (!forecast || forecast.length === 0) return 0;
    const actual = forecast.filter(d => d.actual !== null).reduce((sum, d) => sum + d.actual, 0);
    const projected = forecast.filter(d => d.projected !== null).reduce((sum, d) => sum + d.projected, 0);
    return actual + projected;
  }, [forecast]);

  const totalSavings = recommendations.reduce((sum, r) => sum + r.savings, 0);

  const maxDayCost = Math.max(...forecast.map(d => d.actual || d.projected || 0));

  const getSavingsTypeIcon = (type) => {
    switch (type) {
      case 'compute': return '◉';
      case 'storage': return '▣';
      case 'commitment': return '◈';
      default: return '○';
    }
  };

  return (
    <div className={styles.costPanel}>
      <div className={styles.costHeader}>
        <h3>COST INTELLIGENCE</h3>
        <div className={styles.timeframeBadge}>
          <span className={styles.timeframeLabel}>MTD</span>
        </div>
      </div>

      <div className={styles.costSummary}>
        <div className={styles.summaryCard}>
          <span className={styles.summaryLabel}>Monthly Total</span>
          <span className={styles.summaryValue}>${monthlyTotal.toFixed(2)}</span>
        </div>
        <div className={styles.summaryCard}>
          <span className={styles.summaryLabel}>Weekly Trend</span>
          <span className={styles.summaryValue}>${weeklyTrend.toFixed(2)}</span>
        </div>
        <div className={`${styles.summaryCard} ${styles.savingsCard}`}>
          <span className={styles.summaryLabel}>Potential Savings</span>
          <span className={`${styles.summaryValue} ${styles.savingsValue}`}>${totalSavings.toFixed(2)}</span>
        </div>
      </div>

      <div className={styles.forecastSection}>
        <div className={styles.sectionHeader}>
          <span>7-DAY FORECAST</span>
          <span className={styles.forecastTotal}>${weeklyTrend.toFixed(2)}</span>
        </div>
        <div className={styles.forecastChart}>
          {forecast.map((day, idx) => {
            const value = day.actual || day.projected || 0;
            const height = (value / maxDayCost) * 100;
            return (
              <div key={day.day} className={styles.forecastBar}>
                <div 
                  className={`${styles.barFill} ${day.projected !== null ? styles.projected : ''}`}
                  style={{ height: `${height}%` }}
                >
                  <span className={styles.barValue}>${value.toFixed(0)}</span>
                </div>
                <span className={styles.barLabel}>{day.day}</span>
              </div>
            );
          })}
        </div>
      </div>

      <div className={styles.recommendationsSection}>
        <div className={styles.sectionHeader}>
          <span>RECOMMENDATIONS</span>
          <span className={styles.recCount}>{recommendations.length} actions</span>
        </div>
        <div className={styles.recList}>
          {recommendations.map(rec => (
            <div key={rec.id} className={styles.recItem}>
              <div className={styles.recIcon}>{getSavingsTypeIcon(rec.type)}</div>
              <div className={styles.recContent}>
                <div className={styles.recTitle}>{rec.title}</div>
                <div className={styles.recDesc}>{rec.description}</div>
              </div>
              <div className={styles.recSavings}>
                <span className={styles.savingsAmount}>${rec.savings.toFixed(0)}</span>
                <span className={styles.savingsLabel}>/mo</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
