import { useState } from 'react';
import { crmPost } from '../state/useCrmApi';
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

export default function ShowingLogger({ clientId, houses, onLogged }) {
  const [houseId, setHouseId] = useState('');
  const [outcome, setOutcome] = useState('pending');
  const [feedback, setFeedback] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

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
      await onLogged?.();
    } catch (reason) {
      setError(reason?.message || 'The showing could not be logged.');
    } finally {
      setBusy(false);
    }
  };

  if (options.length === 0) {
    return (
      <p className={styles.empty}>
        No properties are associated with this client yet, so there is nothing to
        record a showing against.
      </p>
    );
  }

  return (
    <form onSubmit={submit}>
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
