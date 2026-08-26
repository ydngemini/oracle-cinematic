import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './DealBook.module.css';

/**
 * The state disclosure checklist for one transaction.
 *
 * `GET /api/compliance/checklist/{id}` has always existed; the write half that
 * materialises rows was added later and never called from anywhere, so this
 * endpoint reported `total_items: 0` on every transaction that has ever
 * existed. Both halves are wired here.
 *
 * The toggles below are not preferences — each one decides whether a disclosure
 * is legally required (year built drives federal lead paint, flood zone drives
 * the state flood disclosure, and so on). They are asked rather than inferred
 * because the transaction record does not carry them and a wrong guess produces
 * a checklist that is confidently missing a required form.
 */

const TRIGGERS = [
  ['has_hoa', 'HOA'],
  ['in_flood_zone', 'Flood zone'],
  ['septic_system', 'Septic'],
  ['well_water', 'Well water'],
  ['is_new_construction', 'New construction'],
  ['seller_known_defects', 'Known defects'],
  ['dual_agency', 'Dual agency'],
];

const STATUS_LABEL = {
  pending: 'Pending',
  delivered: 'Delivered',
  signed: 'Signed',
  waived: 'Waived',
};

function dueLabel(value) {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return null;
  return parsed.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

export default function ComplianceChecklistPanel({ transaction }) {
  const [checklist, setChecklist] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busy, setBusy] = useState(false);
  const [context, setContext] = useState({
    year_built: '',
    buyer_represented: true,
    dual_agency: false,
    is_new_construction: false,
    has_hoa: false,
    in_flood_zone: false,
    septic_system: false,
    well_water: false,
    seller_known_defects: false,
  });

  const transactionId = transaction.id;

  const load = useCallback(() => {
    setLoadError('');
    return crmGet(`/api/compliance/checklist/${transactionId}`).then(
      (payload) => setChecklist(payload || null),
      (reason) => {
        // A transaction with no checklist yet is the normal state, not an error.
        if (reason?.status === 404) {
          setChecklist(null);
          return;
        }
        setLoadError(reason?.message || 'The compliance checklist could not be read.');
      },
    );
  }, [transactionId]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  const materialise = async () => {
    if (busy) return;
    if (!transaction.state_code) {
      setActionError('This transaction has no state recorded, so no state disclosure set applies.');
      return;
    }
    setBusy(true);
    setActionError('');
    const year = Number.parseInt(context.year_built, 10);
    try {
      const payload = await crmPost('/api/compliance/checklist', {
        transaction_id: transactionId,
        context: {
          ...context,
          year_built: Number.isFinite(year) ? year : null,
          state_code: transaction.state_code,
          property_type: transaction.property_type || 'residential_1_4',
          transaction_id: transactionId,
        },
      });
      setChecklist(payload || null);
    } catch (reason) {
      setActionError(reason?.message || 'The checklist could not be built.');
    } finally {
      setBusy(false);
    }
  };

  const items = Array.isArray(checklist?.items) ? checklist.items : [];
  const hasChecklist = (checklist?.total_items || 0) > 0;

  return (
    <section className={styles.offerBar} aria-label="State disclosure checklist">
      <h4>State disclosures</h4>

      {loadError ? <p className={styles.error} role="alert">{loadError}</p> : null}
      {actionError ? <p className={styles.error} role="alert">{actionError}</p> : null}

      {hasChecklist ? (
        <>
          <div className={styles.offerRow}>
            <span className={styles.chip}>{checklist.total_items} required</span>
            <span className={styles.chip}>{checklist.completed} complete</span>
            <span className={styles.chip}>{checklist.pending} pending</span>
            {checklist.overdue > 0 ? (
              <span className={styles.chip} data-tone="bad">{checklist.overdue} overdue</span>
            ) : null}
          </div>
          <ul className={styles.offerList}>
            {items.map((item) => (
              <li key={item.item_id} className={styles.offerItem} data-status={item.status}>
                <div className={styles.offerItemText}>
                  <strong>{item.form_name}</strong>
                  <span className={styles.small}>
                    {STATUS_LABEL[item.status] || item.status}
                    {dueLabel(item.due_date) ? ` · due ${dueLabel(item.due_date)}` : ''}
                    {item.signed_by ? ` · signed by ${item.signed_by}` : ''}
                  </span>
                </div>
              </li>
            ))}
          </ul>
          <div className={styles.offerActions}>
            <button type="button" className={styles.action} onClick={materialise} disabled={busy}>
              {busy ? 'Rechecking…' : 'Recheck requirements'}
            </button>
          </div>
          <p className={styles.small}>
            Rechecking adds disclosures that have become required. It never removes one already
            delivered or signed — that record is evidence of what happened.
          </p>
        </>
      ) : (
        <>
          <p className={styles.small}>
            No checklist built yet. These answers decide which disclosures{' '}
            {transaction.state_code || 'this state'} requires.
          </p>
          <div className={styles.offerRow}>
            <label className={styles.field}>
              <span>Year built</span>
              <input
                className={styles.input}
                value={context.year_built}
                onChange={(event) => setContext((prev) => ({ ...prev, year_built: event.target.value }))}
                inputMode="numeric"
                placeholder="Drives federal lead paint"
              />
            </label>
            <label className={styles.field}>
              <span>Buyer represented</span>
              <input
                type="checkbox"
                checked={context.buyer_represented}
                onChange={(event) => setContext((prev) => ({ ...prev, buyer_represented: event.target.checked }))}
              />
            </label>
            {TRIGGERS.map(([key, label]) => (
              <label key={key} className={styles.field}>
                <span>{label}</span>
                <input
                  type="checkbox"
                  checked={context[key]}
                  onChange={(event) => setContext((prev) => ({ ...prev, [key]: event.target.checked }))}
                />
              </label>
            ))}
            <div className={styles.offerActions}>
              <button type="button" className={styles.action} onClick={materialise} disabled={busy}>
                {busy ? 'Building…' : 'Build checklist'}
              </button>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
