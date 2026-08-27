import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost } from '../state/useCrmApi';
import styles from './AdminOpsTab.module.css';

/**
 * Request and execute a brokerage role change, under two-person approval.
 *
 * `GET /api/admin/permission-policy`, `POST /api/admin/role-changes` and its
 * `/execute` counterpart all shipped with no caller — so the governance the
 * backend enforces had no way to be exercised. Promoting an agent to broker
 * owner could only happen by direct database access, which is exactly the thing
 * an approval trail exists to prevent.
 *
 * The two steps are deliberately separate and shown as such. The server refuses
 * a self-change and refuses to let the requester approve their own request, and
 * every override event is immutable. Collapsing this into one button would
 * misrepresent a control that only means anything because two people touch it.
 */

export default function RoleChangePanel({ users }) {
  const [policy, setPolicy] = useState(null);
  const [userId, setUserId] = useState('');
  const [newRole, setNewRole] = useState('agent');
  const [reason, setReason] = useState('');
  const [approvalId, setApprovalId] = useState('');
  const [executeReason, setExecuteReason] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(() => crmGet('/api/admin/permission-policy').then(
    (payload) => setPolicy(payload || null),
    () => setPolicy(null),
  ), []);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  const act = async (label, run) => {
    if (busy) return;
    setBusy(label);
    setError('');
    setNotice('');
    try {
      await run();
    } catch (reason_) {
      setError(
        reason_?.status === 409
          ? (reason_?.message || 'Refused — a broker cannot change their own role, and a requester cannot approve their own request.')
          : reason_?.message || 'The request was refused.',
      );
    } finally {
      setBusy('');
    }
  };

  const request = () => act('request', async () => {
    const result = await crmPost('/api/admin/role-changes', {
      user_id: userId,
      new_role: newRole,
      reason: reason.trim(),
    });
    const id = result?.approval?.id || result?.approval_id || result?.id || '';
    setApprovalId(id);
    setReason('');
    setNotice(id
      ? 'Requested. A DIFFERENT broker owner or platform admin must now execute it.'
      : 'Requested.');
  });

  const execute = () => act('execute', async () => {
    await crmPost(`/api/admin/role-changes/${approvalId}/execute`, {
      reason: executeReason.trim(),
    });
    setExecuteReason('');
    setApprovalId('');
    setNotice('Executed. The role change is recorded as an immutable override event.');
  });

  const candidates = Array.isArray(users) ? users : [];

  return (
    <>
      {policy ? (
        <p className={styles.quietNote}>
          Two-person approval{policy.requester_may_approve === false ? ', requester may not approve' : ''}
          {policy.reason_required ? ', reason required' : ''}
          {policy.override_events_immutable ? ', override events immutable' : ''}.
          Platform admin is not assignable through this API.
        </p>
      ) : null}

      {error ? <p className={styles.quietNote} role="alert">{error}</p> : null}
      {notice ? <p className={styles.quietNote} role="status">{notice}</p> : null}

      <div className={styles.chipRow}>
        <select value={userId} onChange={(event) => setUserId(event.target.value)} aria-label="User">
          <option value="">Choose a user</option>
          {candidates.map((u) => (
            <option key={u.id || u.agent_id} value={u.id || ''}>
              {u.agent_id}{u.role ? ` · ${u.role}` : ''}
            </option>
          ))}
        </select>
        <select value={newRole} onChange={(event) => setNewRole(event.target.value)} aria-label="New role">
          <option value="agent">agent</option>
          <option value="broker_owner">broker_owner</option>
        </select>
        <input
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Reason (8+ chars)"
          maxLength={500}
          aria-label="Reason for the role change"
        />
        <button
          type="button"
          className={styles.chip}
          onClick={request}
          disabled={!userId || reason.trim().length < 8 || busy !== ''}
        >
          {busy === 'request' ? 'Requesting…' : '1 · Request'}
        </button>
      </div>

      <div className={styles.chipRow}>
        <input
          value={approvalId}
          onChange={(event) => setApprovalId(event.target.value)}
          placeholder="Approval id"
          aria-label="Approval id to execute"
        />
        <input
          value={executeReason}
          onChange={(event) => setExecuteReason(event.target.value)}
          placeholder="Approval reason (8+ chars)"
          maxLength={500}
          aria-label="Reason for approving"
        />
        <button
          type="button"
          className={styles.chip}
          onClick={execute}
          disabled={!approvalId || executeReason.trim().length < 8 || busy !== ''}
        >
          {busy === 'execute' ? 'Executing…' : '2 · Execute'}
        </button>
      </div>
      <p className={styles.quietNote}>
        Step two must be done by someone other than the requester. Signing in as the same person
        and pasting the id back will be refused by the server, not by this form.
      </p>
    </>
  );
}
