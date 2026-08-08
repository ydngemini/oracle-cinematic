import styles from './PanelDataStatus.module.css';

function freshnessLabel(updatedAt) {
  if (!updatedAt) return 'Not loaded';
  return `Updated ${updatedAt.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
}

export function PanelDataStatus({ label, loading, refreshing, error, updatedAt, onRetry }) {
  const state = error ? 'error' : loading || refreshing ? 'loading' : 'ready';
  const detail = error
    ? error.message || 'Source unavailable'
    : loading
      ? 'Loading live data'
      : refreshing
        ? 'Refreshing'
        : freshnessLabel(updatedAt);

  return (
    <li className={styles.source} data-state={state}>
      <span className={styles.indicator} aria-hidden="true" />
      <span className={styles.copy}>
        <strong>{label}</strong>
        <small>{detail}</small>
      </span>
      {error ? (
        <button type="button" onClick={onRetry}>Retry</button>
      ) : null}
    </li>
  );
}

