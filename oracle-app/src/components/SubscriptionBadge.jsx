import { useSubscription } from '../state/useSubscription';
import styles from './SubscriptionBadge.module.css';

export function SubscriptionBadge() {
  const { active, status, plan, currentPeriodEnd, loading, openPortal } = useSubscription();

  if (loading || status === 'none' || status === 'no_db') return null;

  const label = active ? 'LICENSED' : status.toUpperCase();
  const periodEnd = currentPeriodEnd
    ? new Date(currentPeriodEnd).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    : null;

  return (
    <button
      className={styles.badge}
      data-active={active}
      onClick={active ? openPortal : undefined}
      title={active ? 'Manage subscription' : 'Subscription inactive'}
    >
      <span className={styles.dot} data-active={active} />
      <span className={styles.label}>{label}</span>
      {periodEnd && <span className={styles.period}>until {periodEnd}</span>}
    </button>
  );
}
