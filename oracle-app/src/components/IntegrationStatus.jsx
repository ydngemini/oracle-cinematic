import { AlertTriangle, CheckCircle2, Circle, Loader2, XCircle } from 'lucide-react';
import { integrationStatus } from '../lib/integrationStatus';
import styles from './IntegrationStatus.module.css';

const ICONS = {
  ready: CheckCircle2,
  progress: Loader2,
  attention: AlertTriangle,
  problem: XCircle,
  idle: Circle,
};

/**
 * "Phone · Ready", "MLS · Syncing", "Calendar · Reconnect" — icon + words,
 * colour only as reinforcement. Pass `name` to render the integration label
 * too; omit it when the row already names the integration.
 */
export function IntegrationStatus({ integration, state, name, suffix }) {
  const status = integrationStatus(integration, state);
  const Icon = ICONS[status.tone] || Circle;
  return (
    <span className={styles.status} data-tone={status.tone}>
      <Icon className={styles.icon} size={14} aria-hidden="true" />
      {name ? <span className={styles.name}>{name}</span> : null}
      {name ? <span className={styles.sep} aria-hidden="true">·</span> : null}
      <span className={styles.label}>{status.label}</span>
      {suffix}
    </span>
  );
}
