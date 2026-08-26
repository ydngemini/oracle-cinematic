import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPatch, crmPost } from '../state/useCrmApi';
import styles from './DealBook.module.css';

/**
 * Parties and milestones for one transaction.
 *
 * `portfolio_api` has carried both since the deal room was built — who is on
 * the deal, and what has to happen by when — and the UI reached neither, so a
 * transaction opened in this product could record terms and offers but never
 * say who the title company was or whether inspection had cleared.
 *
 * Both lists come from GET /transactions/{id}, which returns them alongside the
 * transaction, so this is one request rather than three.
 */

// Mirrors PartyRole in backend/portfolio_api.py. The API rejects anything else,
// so this list is the contract rather than a suggestion.
const PARTY_ROLES = [
  ['seller', 'Seller'],
  ['buyer', 'Buyer'],
  ['assignor', 'Assignor'],
  ['assignee', 'Assignee'],
  ['agent', 'Agent'],
  ['broker', 'Broker'],
  ['attorney', 'Attorney'],
  ['title', 'Title'],
  ['lender', 'Lender'],
  ['joint_venture', 'Joint venture'],
];

// Mirrors MilestoneUpdate.status. 'waived' and 'cancelled' are distinct on
// purpose: a waived contingency was decided, a cancelled one never applied.
const MILESTONE_STATUSES = [
  ['pending', 'Pending'],
  ['at_risk', 'At risk'],
  ['complete', 'Complete'],
  ['waived', 'Waived'],
  ['cancelled', 'Cancelled'],
];

const MILESTONE_PRESETS = [
  ['inspection', 'Inspection'],
  ['appraisal', 'Appraisal'],
  ['financing', 'Financing commitment'],
  ['title_review', 'Title review'],
  ['walkthrough', 'Final walkthrough'],
  ['closing', 'Closing'],
];

function dueLabel(value) {
  if (!value) return 'No date set';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return 'No date set';
  return parsed.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

// <input type="date"> gives a bare day; the API wants a datetime. Midday avoids
// a timezone shift moving a deadline to the day before.
function asDateTime(value) {
  return value ? `${value}T12:00:00Z` : null;
}

export default function DealRoomPanel({ transactionId }) {
  const [parties, setParties] = useState([]);
  const [milestones, setMilestones] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busy, setBusy] = useState('');

  const [party, setParty] = useState({ role: 'title', name: '' });
  const [milestone, setMilestone] = useState({ type: 'inspection', title: '', due: '', assignee: '' });

  const load = useCallback(() => {
    setLoading(true);
    setLoadError('');
    return crmGet(`/api/portfolio/transactions/${transactionId}`).then(
      (payload) => {
        setParties(Array.isArray(payload?.parties) ? payload.parties : []);
        setMilestones(Array.isArray(payload?.milestones) ? payload.milestones : []);
        setLoading(false);
      },
      (reason) => {
        setLoadError(reason?.message || 'The deal room could not be loaded.');
        setLoading(false);
      },
    );
  }, [transactionId]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  const addParty = async () => {
    const displayName = party.name.trim();
    if (!displayName || busy) return;
    setBusy('party');
    setActionError('');
    try {
      await crmPost(`/api/portfolio/transactions/${transactionId}/parties`, {
        party_role: party.role,
        display_name: displayName,
      });
      setParty((prev) => ({ ...prev, name: '' }));
      await load();
    } catch (reason) {
      setActionError(reason?.message || 'The party could not be added.');
    } finally {
      setBusy('');
    }
  };

  const addMilestone = async () => {
    const title = milestone.title.trim()
      || MILESTONE_PRESETS.find(([value]) => value === milestone.type)?.[1]
      || '';
    if (!title || busy) return;
    setBusy('milestone');
    setActionError('');
    const payload = { milestone_type: milestone.type, title };
    const due = asDateTime(milestone.due);
    if (due) payload.due_at = due;
    if (milestone.assignee.trim()) payload.assigned_to = milestone.assignee.trim();
    try {
      await crmPost(`/api/portfolio/transactions/${transactionId}/milestones`, payload);
      setMilestone({ type: 'inspection', title: '', due: '', assignee: '' });
      await load();
    } catch (reason) {
      setActionError(reason?.message || 'The milestone could not be added.');
    } finally {
      setBusy('');
    }
  };

  const setMilestoneStatus = async (row, status) => {
    if (busy) return;
    setBusy(row.id);
    setActionError('');
    // Optimistic: a status dropdown that waits for a round trip feels broken.
    // Reverted from the server's answer on failure rather than left guessing.
    const previous = milestones;
    setMilestones((rows) => rows.map((item) => (
      item.id === row.id ? { ...item, status } : item
    )));
    try {
      await crmPatch(`/api/portfolio/milestones/${row.id}`, { status });
    } catch (reason) {
      setMilestones(previous);
      setActionError(reason?.message || 'The milestone status could not be changed.');
    } finally {
      setBusy('');
    }
  };

  return (
    <section className={styles.offerBar} aria-label="Deal room">
      <h4>Parties &amp; milestones</h4>

      {loading ? <p className={styles.small}>Loading deal room…</p> : null}
      {loadError ? <p className={styles.error} role="alert">{loadError}</p> : null}
      {actionError ? <p className={styles.error} role="alert">{actionError}</p> : null}

      {!loading && !loadError ? (
        <>
          <div className={styles.offerRow}>
            <label className={styles.field}>
              <span>Role</span>
              <select
                className={styles.input}
                value={party.role}
                onChange={(event) => setParty((prev) => ({ ...prev, role: event.target.value }))}
              >
                {PARTY_ROLES.map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </label>
            <label className={styles.field}>
              <span>Name</span>
              <input
                className={styles.input}
                value={party.name}
                onChange={(event) => setParty((prev) => ({ ...prev, name: event.target.value }))}
                placeholder="First American Title"
              />
            </label>
            <div className={styles.offerActions}>
              <button
                type="button"
                className={styles.action}
                onClick={addParty}
                disabled={!party.name.trim() || busy === 'party'}
              >
                {busy === 'party' ? 'Adding…' : 'Add party'}
              </button>
            </div>
          </div>

          {parties.length === 0 ? (
            <p className={styles.small}>No parties recorded yet.</p>
          ) : (
            <ul className={styles.offerList}>
              {parties.map((row) => (
                <li key={row.id} className={styles.offerItem}>
                  <div className={styles.offerItemText}>
                    <strong>{row.display_name}</strong>
                    <span className={styles.small}>
                      {PARTY_ROLES.find(([value]) => value === row.party_role)?.[1] || row.party_role}
                      {row.verified_at ? ' · verified' : ' · unverified'}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}

          <div className={styles.offerRow}>
            <label className={styles.field}>
              <span>Milestone</span>
              <select
                className={styles.input}
                value={milestone.type}
                onChange={(event) => setMilestone((prev) => ({ ...prev, type: event.target.value }))}
              >
                {MILESTONE_PRESETS.map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </label>
            <label className={styles.field}>
              <span>Title</span>
              <input
                className={styles.input}
                value={milestone.title}
                onChange={(event) => setMilestone((prev) => ({ ...prev, title: event.target.value }))}
                placeholder="Defaults to the milestone name"
              />
            </label>
            <label className={styles.field}>
              <span>Due</span>
              <input
                className={styles.input}
                type="date"
                value={milestone.due}
                onChange={(event) => setMilestone((prev) => ({ ...prev, due: event.target.value }))}
              />
            </label>
            <label className={styles.field}>
              <span>Owner</span>
              <input
                className={styles.input}
                value={milestone.assignee}
                onChange={(event) => setMilestone((prev) => ({ ...prev, assignee: event.target.value }))}
                placeholder="Who owns it"
              />
            </label>
            <div className={styles.offerActions}>
              <button
                type="button"
                className={styles.action}
                onClick={addMilestone}
                disabled={busy === 'milestone'}
              >
                {busy === 'milestone' ? 'Adding…' : 'Add milestone'}
              </button>
            </div>
          </div>

          {milestones.length === 0 ? (
            <p className={styles.small}>No milestones set — nothing is being tracked to a date.</p>
          ) : (
            <ul className={styles.offerList}>
              {milestones.map((row) => (
                <li key={row.id} className={styles.offerItem} data-status={row.status}>
                  <div className={styles.offerItemText}>
                    <strong>{row.title}</strong>
                    <span className={styles.small}>
                      {dueLabel(row.due_at)}
                      {row.assigned_to ? ` · ${row.assigned_to}` : ''}
                    </span>
                  </div>
                  <div className={styles.offerActions}>
                    <label className={styles.field}>
                      <span className={styles.small}>Status</span>
                      <select
                        className={styles.input}
                        value={row.status}
                        disabled={busy === row.id}
                        onChange={(event) => setMilestoneStatus(row, event.target.value)}
                        aria-label={`Status for ${row.title}`}
                      >
                        {MILESTONE_STATUSES.map(([value, label]) => (
                          <option key={value} value={value}>{label}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : null}
    </section>
  );
}
