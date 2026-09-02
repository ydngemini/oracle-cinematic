import { useCallback, useEffect, useState } from 'react';
import { Lock } from 'lucide-react';

import { crmGet, crmPut } from '../state/useCrmApi';
import styles from './AutonomyControls.module.css';

/**
 * The autonomy dial — per capability, with the ceilings shown rather than hidden.
 *
 * One global "AI on/off" asks the wrong question. An agent may be glad for Neoh
 * to file notes unattended and never want it near a counter-offer; collapsing
 * both into one switch means the cautious answer disables the useful half, and
 * the switch ends up permanently off.
 *
 * The design decision that matters here is showing the locked rows instead of
 * omitting them. A category that cannot be automated is more reassuring visible
 * — it demonstrates the product has limits it enforces on itself — than absent,
 * which just looks like a missing feature. The lock states its reason inline.
 *
 * Those ceilings are enforced by CHECK constraints in migration 0095, not by
 * this component. This is a mirror of them, and the API returns a 403 with an
 * explanation if the two ever drift.
 */

const LEVEL_COPY = {
  observe: { label: 'Observe', detail: 'Analyses and recommends. Touches nothing.' },
  assist: { label: 'Assist', detail: 'Prepares the work. You release it.' },
  autopilot: { label: 'Autopilot', detail: 'Acts within your rules.' },
};

function CategoryRow({ entry, onChange, busy }) {
  const locked = entry.permitted.length === 1;
  const groupId = `autonomy-${entry.category}`;

  return (
    <li className={styles.row}>
      <div className={styles.rowHead}>
        <div>
          <h3 className={styles.rowLabel} id={groupId}>
            {entry.label}
            {locked && <Lock aria-hidden="true" size={12} className={styles.lockIcon} />}
          </h3>
          <p className={styles.rowDetail}>{entry.detail}</p>
        </div>
        {entry.is_default && <span className={styles.defaultTag}>default</span>}
      </div>

      <div className={styles.levels} role="radiogroup" aria-labelledby={groupId}>
        {['observe', 'assist', 'autopilot'].map((level) => {
          const permitted = entry.permitted.includes(level);
          const active = entry.level === level;
          return (
            <button
              type="button"
              key={level}
              role="radio"
              aria-checked={active}
              disabled={!permitted || busy}
              className={`${styles.level} ${active ? styles.levelActive : ''} ${!permitted ? styles.levelBlocked : ''}`}
              onClick={() => permitted && !active && onChange(entry.category, level)}
              title={permitted ? LEVEL_COPY[level].detail : entry.ceiling_reason || undefined}
            >
              {LEVEL_COPY[level].label}
            </button>
          );
        })}
      </div>

      {/* The reason is always visible, never a tooltip. A limit an agent can
          see the logic of is a limit they trust; an unexplained greyed-out
          button reads as a bug or an upsell. */}
      {entry.ceiling_reason && (
        <p className={styles.ceilingReason}>{entry.ceiling_reason}</p>
      )}
    </li>
  );
}

export function AutonomyControls() {
  const [settings, setSettings] = useState(null);
  const [status, setStatus] = useState('loading');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);

  const load = useCallback(async (isCancelled = () => false) => {
    try {
      const data = await crmGet('/api/autonomy');
      if (!isCancelled()) { setSettings(data); setStatus('ready'); }
    } catch {
      if (!isCancelled()) setStatus('error');
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const frame = window.requestAnimationFrame(() => { void load(() => cancelled); });
    return () => { cancelled = true; window.cancelAnimationFrame(frame); };
  }, [load]);

  const change = useCallback(async (category, level) => {
    setBusy(true);
    setNotice(null);
    try {
      await crmPut('/api/autonomy', { category, level });
      await load();
    } catch (error) {
      // The server's refusal text explains the ceiling. Replacing it with a
      // generic "could not save" would throw away the only useful part.
      setNotice(error?.message || 'That setting did not save.');
    } finally {
      setBusy(false);
    }
  }, [load]);

  if (status === 'loading') {
    return <div className={styles.shell} aria-busy="true" aria-label="Loading autonomy settings">
      <div className={styles.skeleton} /><div className={styles.skeleton} />
    </div>;
  }

  if (status === 'error') {
    return <div className={styles.shell}>
      <p className={styles.error} role="alert">Could not load autonomy settings.</p>
    </div>;
  }

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <h2 className={styles.title}>What Neoh may do on its own</h2>
        <p className={styles.subtitle}>
          Set per capability, not once for everything. Some categories are
          capped and cannot be raised — those limits are enforced by the
          database, not by this screen.
        </p>
      </header>

      {notice && <p className={styles.notice} role="alert">{notice}</p>}

      <ul className={styles.list}>
        {settings?.categories?.map((entry) => (
          <CategoryRow entry={entry} key={entry.category} onChange={change} busy={busy} />
        ))}
      </ul>
    </div>
  );
}

export default AutonomyControls;
