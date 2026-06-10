import { useOracleState } from '../state';
import styles from './LivePulse.module.css';

// Entries arrive newest-first from the reducer (FEED_EVENT prepends, cap 50).
// Every card is derived from a real Nexus Bus frame in useOracleWebSocket —
// this panel renders state, it never generates events.

function formatClock(ts) {
  const d = new Date(ts);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, '0'))
    .join(':');
}

export function LivePulse() {
  const { liveFeed } = useOracleState();
  const isEmpty = liveFeed.length === 0;

  return (
    <section className={styles.panel} aria-label="Live activity pulse">
      <header className={styles.header}>
        <span className={styles.pulse} data-idle={isEmpty} aria-hidden="true" />
        <span className={styles.kicker}>Live Pulse</span>
        {!isEmpty && <span className={styles.count}>{liveFeed.length}</span>}
      </header>

      <div className={styles.body} role="log" aria-live="polite">
        {isEmpty ? (
          <p className={styles.idle}>Listening on the bus…</p>
        ) : (
          <ul className={styles.list}>
            {liveFeed.map((entry) => (
              <li
                key={entry.id}
                className={styles.card}
                data-actor={entry.actor || 'ai'}
              >
                <span className={styles.cardTop}>
                  <span className={styles.actorName}>{entry.actorName}</span>
                  {entry.tag && <span className={styles.tag}>{entry.tag}</span>}
                  <time
                    className={styles.time}
                    dateTime={new Date(entry.timestamp).toISOString()}
                  >
                    {formatClock(entry.timestamp)}
                  </time>
                </span>

                <span className={styles.text}>
                  {entry.text}
                  {entry.metric && (
                    <span className={styles.metric}>{entry.metric}</span>
                  )}
                </span>

                {entry.href && (
                  <a className={styles.inspect} href={entry.href}>
                    Inspect <span aria-hidden="true">→</span>
                  </a>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
