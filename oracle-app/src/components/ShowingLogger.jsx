import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPatch, crmPost } from '../state/useCrmApi';
import styles from './ClientDetailDrawer.module.css';

/**
 * Log that a client was shown a property.
 *
 * `POST /api/crm/showings` had no caller, so the drawer could DISPLAY a client's
 * showing history — the detail endpoint has always returned it — while nothing
 * in the product could add to it. A showing is the buyer↔property exposure
 * record the whole pipeline reads from, so an unrecordable one is a hole in the
 * history rather than a missing convenience.
 *
 * The property list is the client's own `houses` rollup, which already carries
 * the `kind` that decides whether this becomes a lead_id or a listing_id — the
 * server requires exactly one of them.
 */

// Mirrors chk_showing_outcome in migration 0012. Anything else is rejected.
const OUTCOMES = [
  ['pending', 'Pending'],
  ['interested', 'Interested'],
  ['offer_made', 'Offer made'],
  ['passed', 'Passed'],
  ['no_show', 'No show'],
];

/**
 * The showings already logged for this client, each with a live outcome
 * control. Until PATCH /showings/{id} existed a showing was write-once — logged
 * as 'pending' before the buyer had reacted, and 'pending' forever after. A
 * resolved showing is the one exposure record Outcome Memory learns from.
 */
function RecentShowings({ clientId, reloadKey, onResolved }) {
  const [rows, setRows] = useState(null);
  const [savingId, setSavingId] = useState('');
  const [error, setError] = useState('');

  const load = useCallback(async (isCancelled = () => false) => {
    try {
      const data = await crmGet(`/api/crm/clients/${clientId}/showings?limit=10`);
      if (!isCancelled()) setRows(Array.isArray(data?.showings) ? data.showings : []);
    } catch {
      if (!isCancelled()) setRows([]);
    }
  }, [clientId]);

  useEffect(() => {
    if (!clientId) return undefined;
    let cancelled = false;
    const frame = window.requestAnimationFrame(() => { void load(() => cancelled); });
    return () => { cancelled = true; window.cancelAnimationFrame(frame); };
  }, [clientId, load, reloadKey]);

  const resolve = async (id, outcome) => {
    setSavingId(id);
    setError('');
    try {
      await crmPatch(`/api/crm/showings/${id}`, { outcome });
      await load();
      await onResolved?.();
    } catch (reason) {
      setError(reason?.message || 'The showing could not be updated.');
    } finally {
      setSavingId('');
    }
  };

  if (!rows || rows.length === 0) return null;

  return (
    <div className={styles.field}>
      <span>Recent showings</span>
      {error ? <p className={styles.errorText} role="alert">{error}</p> : null}
      <ul className={styles.plainList}>
        {rows.map((row) => (
          <li key={row.id} className={styles.showingRow}>
            <span className={styles.showingAddress}>
              {row.address || row.listing_id || row.lead_id}
              {row.shown_at && (
                <time dateTime={row.shown_at} className={styles.showingWhen}>
                  {' · '}{new Date(row.shown_at).toLocaleDateString()}
                </time>
              )}
            </span>
            <select
              className={styles.input}
              value={row.outcome}
              disabled={savingId === row.id}
              aria-label={`Outcome for showing at ${row.address || 'property'}`}
              onChange={(event) => resolve(row.id, event.target.value)}
            >
              {OUTCOMES.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function ShowingLogger({ clientId, houses, onLogged }) {
  const [houseId, setHouseId] = useState('');
  const [outcome, setOutcome] = useState('pending');
  const [feedback, setFeedback] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  // Bumped after a successful log so the list below re-reads.
  const [logged, setLogged] = useState(0);

  const options = Array.isArray(houses) ? houses.filter((h) => h?.id) : [];

  const submit = async (event) => {
    event.preventDefault();
    if (!clientId || !houseId || busy) return;
    const house = options.find((h) => h.id === houseId);
    if (!house) return;

    setBusy(true);
    setError('');
    setNotice('');
    const payload = { client_id: clientId, outcome };
    // Exactly one of these — the server enforces it, and sending both would be
    // a 422 rather than a helpfully-ignored extra.
    if (house.kind === 'listing') payload.listing_id = house.id;
    else payload.lead_id = house.id;
    if (feedback.trim()) payload.feedback = feedback.trim();

    try {
      await crmPost('/api/crm/showings', payload);
      setFeedback('');
      setOutcome('pending');
      setNotice('Showing logged.');
      setLogged((n) => n + 1);
      await onLogged?.();
    } catch (reason) {
      setError(reason?.message || 'The showing could not be logged.');
    } finally {
      setBusy(false);
    }
  };

  if (options.length === 0) {
    return (
      <>
        <RecentShowings clientId={clientId} reloadKey={logged} onResolved={onLogged} />
        <p className={styles.empty}>
          No properties are associated with this client yet, so there is nothing to
          record a showing against.
        </p>
      </>
    );
  }

  return (
    <form onSubmit={submit}>
      <RecentShowings clientId={clientId} reloadKey={logged} onResolved={onLogged} />
      {error ? <p className={styles.errorText} role="alert">{error}</p> : null}
      {notice ? <p role="status">{notice}</p> : null}

      <label className={styles.field}>
        <span>Property</span>
        <select
          className={styles.input}
          value={houseId}
          onChange={(event) => setHouseId(event.target.value)}
          aria-label="Property shown"
        >
          <option value="">Choose a property</option>
          {options.map((house) => (
            <option key={house.id} value={house.id}>
              {house.address || house.id} · {house.kind}
            </option>
          ))}
        </select>
      </label>

      <label className={styles.field}>
        <span>Outcome</span>
        <select
          className={styles.input}
          value={outcome}
          onChange={(event) => setOutcome(event.target.value)}
        >
          {OUTCOMES.map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </label>

      <label className={styles.field}>
        <span>Feedback</span>
        <input
          className={styles.input}
          value={feedback}
          onChange={(event) => setFeedback(event.target.value)}
          maxLength={4000}
          placeholder="What they said about it"
        />
      </label>

      <button type="submit" disabled={!houseId || busy}>
        {busy ? 'Logging…' : 'Log showing'}
      </button>
    </form>
  );
}
