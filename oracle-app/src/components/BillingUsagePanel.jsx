import { useCallback, useEffect, useState } from 'react';
import { crmGet } from '../state/useCrmApi';
import styles from './AdminOpsTab.module.css';

/**
 * What this tenant actually consumed, and whether any of it is billable yet.
 *
 * `GET /api/billing/usage` shipped with no caller. Usage has been accruing into
 * billing_usage_events the whole time — the endpoint's own comment explains that
 * consumption is recorded regardless of pricing model, precisely so the history
 * exists before anyone turns metering on. Nothing ever read it back.
 *
 * The honesty rule here is the endpoint's, and this screen keeps it: while
 * `metered_billing_enabled` is false the plan is flat, and none of these numbers
 * is a charge. Showing quantities without that caveat would read as a bill.
 * `reported_to_stripe` is shown separately for the same reason — a recorded
 * event and a reported one are different facts, and the gap between them is
 * what tells you the drain is working.
 */

function when(value) {
  if (!value) return 'never';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return 'never';
  return parsed.toLocaleString();
}

const WINDOWS = [7, 30, 90];

export default function BillingUsagePanel() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  const load = useCallback(() => {
    setError('');
    return crmGet(`/api/billing/usage?days=${days}`).then(
      (payload) => setData(payload || null),
      (reason) => setError(
        reason?.status === 403
          ? 'Only a broker owner can see billing usage.'
          : reason?.message || 'Usage could not be read.',
      ),
    );
  }, [days]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  const metrics = Array.isArray(data?.metrics) ? data.metrics : [];
  const metered = data?.metered_billing_enabled;
  const unreported = metrics.reduce(
    (sum, m) => sum + Math.max(0, (m.events || 0) - (m.reported_to_stripe || 0)),
    0,
  );

  return (
    <>
      <div className={styles.chipRow}>
        {WINDOWS.map((n) => (
          <button
            key={n}
            type="button"
            className={styles.chip}
            aria-pressed={days === n}
            onClick={() => setDays(n)}
          >
            {n}d
          </button>
        ))}
      </div>

      {error ? <p className={styles.quietNote} role="alert">{error}</p> : null}

      {data && metrics.length === 0 ? (
        <p className={styles.quietNote}>
          No usage recorded in this window.
        </p>
      ) : null}

      {metrics.length > 0 ? (
        <ul className={styles.rowList} role="list">
          {metrics.map((m) => (
            <li key={m.metric} className={styles.userRow}>
              <div className={styles.rowMain}>
                <span className={styles.rowTitle}>{m.metric.replace(/_/g, ' ')}</span>
                <span className={styles.rowSub}>
                  {m.events} event{m.events === 1 ? '' : 's'} ·
                  {' '}{m.reported_to_stripe} reported · last {when(m.last_at)}
                </span>
              </div>
              <span className={styles.statusChip}>{m.quantity}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {data ? (
        <p className={styles.quietNote}>
          {metered
            ? `Metered billing is on — these quantities are what Stripe is being told.${
              unreported > 0 ? ` ${unreported} event(s) recorded but not yet reported; the drain runs on the scheduler.` : ''
            }`
            : 'Metered billing is OFF, so the plan is flat and none of this is a charge. Usage is recorded anyway, which is what makes a later pricing change possible without inventing history.'}
        </p>
      ) : null}
    </>
  );
}
