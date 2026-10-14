import { useState } from 'react';
import { crmDelete, crmPost } from '../state/useCrmApi';
import { friendlyError, isHumanMessage } from '../lib/errorMessages';
import styles from './ClientDetailDrawer.module.css';

/*
 * Who sees and changes a client or a contact (owner decision D-2026-10-13-01):
 *   - no assignee = the brokerage pool, which every agent sees and edits;
 *   - the assignee and the broker owner see it, and anyone it is shared with;
 *   - a share is "view only" or "can edit", chosen per share.
 * The server decides (RLS + /api/crm/{clients,contacts}/{id}/access); this only shows it
 * and offers the moves the server allows.
 */

// The server's 403s on these routes are written for the agent ("Only the
// broker owner can …"); show them rather than a generic "no access".
function accessError(err, fallback) {
  if (err?.status === 403 && isHumanMessage(err.message)) return err.message.trim();
  return friendlyError(err, { fallback });
}

const nameOf = (people, agentId) => {
  if (!agentId) return null;
  const hit = people.find((p) => p.agent_id?.toLowerCase() === String(agentId).toLowerCase());
  return hit ? (hit.is_me ? 'You' : hit.name) : agentId;
};

/** Header cell: the assignee, and the assignment moves this caller may make. */
export function AssigneeControl({ state, assigneeId: fromRecord, onAssign, noun = 'client' }) {
  const { access, colleagues, me, isOwner, loading } = state;
  if (loading && !access) return <span className={styles.accessNote}>…</span>;
  // The drawer's seed card may not carry the assignee yet; /access always does.
  const assigneeId = fromRecord ?? access?.assignee_id ?? null;
  const current = nameOf(colleagues, assigneeId);
  if (isOwner) {
    return (
      <select
        className={styles.assigneeInput}
        value={colleagues.find((p) => p.agent_id?.toLowerCase() === String(assigneeId || '').toLowerCase())?.agent_id || ''}
        onChange={(e) => onAssign(e.target.value || null)}
        aria-label={`Assign this ${noun}`}
      >
        <option value="">Brokerage pool (unassigned)</option>
        {colleagues.map((p) => <option key={p.user_id} value={p.agent_id}>{p.is_me ? `${p.name} (you)` : p.name}</option>)}
      </select>
    );
  }
  const mine = me && assigneeId && me.agent_id?.toLowerCase() === String(assigneeId).toLowerCase();
  return (
    <span className={styles.assigneeRow}>
      <span className={styles.assigneeName}>{current || 'Brokerage pool'}</span>
      {!assigneeId && access?.access === 'edit' && me && (
        <button type="button" className={styles.smallBtn} onClick={() => onAssign(me.agent_id)}>Take it</button>
      )}
      {mine && (
        <button type="button" className={styles.smallBtn} onClick={() => onAssign(null)}>Return to pool</button>
      )}
    </span>
  );
}

/** Who else can see this record, and sharing it. `kind` is the API collection. */
export function RecordSharing({ kind = 'clients', noun = 'client', recordId, state, assigneeId, onChanged }) {
  const { access, colleagues, me, error, reload } = state;
  const [grantee, setGrantee] = useState('');
  const [permission, setPermission] = useState('view');
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');

  if (error) {
    return (
      <section className={styles.section} aria-label="Sharing">
        <span className={styles.sectionLabel}>{`Who can see this ${noun}`}</span>
        <p className={styles.accessNote} role="alert">{friendlyError(error, { fallback: 'Sharing could not be loaded.' })}</p>
      </section>
    );
  }
  if (!access) return null;

  const mine = me && assigneeId && me.agent_id?.toLowerCase() === String(assigneeId).toLowerCase();
  const sharer = nameOf(colleagues, assigneeId || access.assignee_id);
  const sharedWithMe = !access.pool && !access.can_manage_shares && !mine;
  const candidates = colleagues.filter((p) => p.role === 'agent' && !p.is_me
    && p.agent_id?.toLowerCase() !== String(assigneeId || '').toLowerCase());

  const share = async (e) => {
    e.preventDefault();
    if (!grantee) return;
    setBusy('share'); setMessage('');
    try {
      await crmPost(`/api/crm/${kind}/${recordId}/shares`, { grantee, permission });
      setGrantee('');
      reload(); onChanged?.();
    } catch (err) {
      setMessage(accessError(err, `The ${noun} could not be shared.`));
    } finally { setBusy(''); }
  };
  const revoke = async (shareId) => {
    setBusy(shareId); setMessage('');
    try {
      await crmDelete(`/api/crm/${kind}/${recordId}/shares/${shareId}`);
      reload(); onChanged?.();
    } catch (err) {
      setMessage(accessError(err, 'Sharing could not be stopped.'));
    } finally { setBusy(''); }
  };

  return (
    <section className={styles.section} aria-label="Sharing">
      <span className={styles.sectionLabel}>{`Who can see this ${noun}`}</span>
      {access.pool && (
        <p className={styles.accessNote}>{`In the brokerage pool: everyone in the brokerage can see and edit this ${noun}. Assign it to share it with specific people.`}</p>
      )}
      {sharedWithMe && (
        <p className={styles.accessNote} data-access={access.access}>
          {/* The drawer's seed card may not carry the assignee yet; /access does. */}
          {`Shared with you${sharer ? ` by ${sharer}` : ''}: ${access.access === 'edit' ? 'you can edit' : 'view only'}.`}
        </p>
      )}
      {access.can_manage_shares && !access.pool && (
        <>
          <p className={styles.accessNote}>{sharer ? `${sharer} and the broker owner` : 'The assigned agent and the broker owner'}{access.shares.length ? ', plus:' : '. Nobody else.'}</p>
          {access.shares.length > 0 && (
            <ul className={styles.shareList}>
              {access.shares.map((s) => (
                <li key={s.id} className={styles.shareRow}>
                  <span>{s.grantee_name || s.grantee_agent_id}</span>
                  <span className={styles.dataKey}>{s.permission === 'edit' ? 'Can edit' : 'View only'}</span>
                  <button type="button" className={styles.smallBtn} disabled={busy === s.id}
                    onClick={() => revoke(s.id)} aria-label={`Stop sharing with ${s.grantee_name || s.grantee_agent_id}`}>
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          {candidates.length > 0 ? (
            <form className={styles.shareForm} onSubmit={share}>
              <label className={styles.field}>
                <span className={styles.microLabel}>Share with</span>
                <select className={styles.input} value={grantee} onChange={(e) => setGrantee(e.target.value)}>
                  <option value="">Choose an agent</option>
                  {candidates.map((p) => <option key={p.user_id} value={p.agent_id}>{p.name}</option>)}
                </select>
              </label>
              <label className={styles.field}>
                <span className={styles.microLabel}>Access</span>
                <select className={styles.input} value={permission} onChange={(e) => setPermission(e.target.value)}>
                  <option value="view">View only</option>
                  <option value="edit">Can edit</option>
                </select>
              </label>
              <button type="submit" className={styles.logBtn} disabled={!grantee || busy === 'share'}>Share</button>
            </form>
          ) : (
            <p className={styles.accessNote}>There is no other agent in the brokerage to share with yet.</p>
          )}
        </>
      )}
      {message && <p className={styles.accessNote} role="alert">{message}</p>}
    </section>
  );
}

/** The client drawer's Overview section (0131). */
export default function ClientSharing({ clientId, ...rest }) {
  return <RecordSharing kind="clients" noun="client" recordId={clientId} {...rest} />;
}
