import { useMemo } from 'react';
import { IndicatorLight } from '../RetroGauges.jsx';
import styles from './SecurityDashboard.module.css';

const severityLevels = {
  critical: { color: '#ff3b5c', weight: 10 },
  high: { color: '#ff9f1a', weight: 7 },
  medium: { color: '#fcd34d', weight: 4 },
  low: { color: '#00d4ff', weight: 1 },
  info: { color: '#a855f7', weight: 0 },
};

const defaultFindings = [
  { id: 1, title: 'Unrestricted Security Group', severity: 'critical', resource: 'sg-0abc123', description: 'Port 22 open to 0.0.0.0/0' },
  { id: 2, title: 'Encrypt EBS Volumes', severity: 'high', resource: 'vol-xyz789', description: 'Volume not encrypted at rest' },
  { id: 3, title: 'IAM Key Rotation Due', severity: 'medium', resource: 'iam-user-prod', description: 'Access key older than 90 days' },
  { id: 4, title: 'Missing CloudWatch Alarms', severity: 'low', resource: 'prod-rds', description: 'No alarms configured for CPU usage' },
  { id: 5, title: 'S3 Bucket Logging', severity: 'info', resource: 'data-bucket-prod', description: 'Server access logging not enabled' },
];

export default function SecurityDashboard({ ec2, rds, findings = defaultFindings }) {
  const securityScore = useMemo(() => {
    const maxScore = 100;
    const deductions = findings.reduce((acc, f) => {
      return acc + severityLevels[f.severity]?.weight || 0;
    }, 0);
    return Math.max(0, maxScore - Math.min(50, deductions * 2));
  }, [findings]);

  const summary = useMemo(() => {
    return {
      critical: findings.filter(f => f.severity === 'critical').length,
      high: findings.filter(f => f.severity === 'high').length,
      medium: findings.filter(f => f.severity === 'medium').length,
      low: findings.filter(f => f.severity === 'low').length,
    };
  }, [findings]);

  const getScoreColor = () => {
    if (securityScore >= 80) return '#00ff88';
    if (securityScore >= 60) return '#ff9f1a';
    return '#ff3b5c';
  };

  const getScoreLabel = () => {
    if (securityScore >= 80) return 'SECURE';
    if (securityScore >= 60) return 'AT RISK';
    return 'CRITICAL';
  };

  return (
    <div className={styles.securityDashboard}>
      <div className={styles.securityHeader}>
        <h3>SECURITY POSTURE</h3>
        <IndicatorLight 
          status={securityScore >= 80 ? 'ok' : securityScore >= 60 ? 'warning' : 'critical'} 
          label={getScoreLabel()} 
        />
      </div>

      <div className={styles.scoreSection}>
        <div className={styles.scoreCircle}>
          <svg viewBox="0 0 100 100" className={styles.scoreRing}>
            <circle
              cx="50"
              cy="50"
              r="45"
              fill="none"
              stroke="rgba(0, 212, 255, 0.1)"
              strokeWidth="8"
            />
            <circle
              cx="50"
              cy="50"
              r="45"
              fill="none"
              stroke={getScoreColor()}
              strokeWidth="8"
              strokeLinecap="round"
              strokeDasharray={`${securityScore * 2.83} 283`}
              className={styles.scoreArc}
            />
          </svg>
          <div className={styles.scoreValue}>
            <span className={styles.scoreNumber} style={{ color: getScoreColor() }}>
              {securityScore}
            </span>
          </div>
        </div>
        
        <div className={styles.severitySummary}>
          {Object.entries(summary).map(([severity, count]) => (
            <div 
              key={severity} 
              className={styles.severityItem}
              style={{ '--severity-color': severityLevels[severity]?.color }}
            >
              <span className={styles.severityCount}>{count}</span>
              <span className={styles.severityLabel}>{severity.toUpperCase()}</span>
            </div>
          ))}
        </div>
      </div>

      <div className={styles.findingsHeader}>
        <span>ACTIVE FINDINGS</span>
        <span className={styles.findingCount}>{findings.length} total</span>
      </div>

      <div className={styles.findingsList}>
        {findings.map(finding => (
          <div 
            key={finding.id}
            className={styles.findingItem}
            style={{ borderColor: severityLevels[finding.severity]?.color }}
          >
            <div 
              className={styles.severityIndicator}
              style={{ background: severityLevels[finding.severity]?.color }}
            />
            <div className={styles.findingContent}>
              <div className={styles.findingTitle}>{finding.title}</div>
              <div className={styles.findingMeta}>
                <span className={styles.resourceId}>{finding.resource}</span>
                <span className={styles.findingDesc}>{finding.description}</span>
              </div>
            </div>
            <div 
              className={styles.severityBadge}
              style={{ 
                background: `${severityLevels[finding.severity]?.color}20`,
                color: severityLevels[finding.severity]?.color 
              }}
            >
              {finding.severity.toUpperCase()}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
