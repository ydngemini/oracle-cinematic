import { useState } from 'react';
import { crmPost } from '../state/useCrmApi';
import styles from './AccountDataPanel.module.css';
import { friendlyError } from '../lib/errorMessages';

// Offboard one agent: pick who takes over, see exactly what will move (a dry
// run of the real thing), then confirm with your password. Their notes,
// messages and history keep their name — only open work changes hands.
// Their Google connection is switched off at once, but removing it at Google
// can lag (Google down): the server says so (external_revocation) and this
// form says it is pending instead of claiming it is gone (audit 22.7).

const LABELS = {
  agent_contacts: 'contacts', clients: 'clients', client_tasks: 'open tasks',
  intake_handoff_tasks: 'handoff tasks', transaction_milestones: 'milestones',
  smart_plans: 'plans', hyperlocal_sites: 'sites', ai_record_attachments: 'attachments',
  client_segments: 'segments', lead_routing_rules: 'routing rules',
};

// Which connection is still waiting, from the server's per-credential
// outcomes: only a Google grant has a provider-side removal to confirm.
const PENDING_STATUSES = ['revoke_failed', 'revocation_unverified'];
function pendingSentence(result) {
  const providers = new Set((result?.credential_revocation || [])
    .filter((r) => PENDING_STATUSES.includes(r.status)).map((r) => r.provider));
  if (providers.size === 1 && providers.has('google')) {
    return 'Their Google connection is switched off in Neoh, but Google has not yet confirmed it is removed.';
  }
  return 'Their connected accounts are switched off in Neoh, but removing them is not confirmed yet.';
}

const summarise = (counts) => Object.entries(counts || {})
  .filter(([, n]) => n > 0)
  .map(([k, n]) => `${n} ${LABELS[k] || k.replace(/_/g, ' ')}`)
  .join(', ');

export function OffboardMemberForm({ member, members, onDone, onCancel }) {
  const successors = members.filter((m) => m.status === 'active' && m.agent_id !== member.agent_id);
  const [successor, setSuccessor] = useState(successors[0]?.agent_id || '');
  const [reason, setReason] = useState('');
  const [password, setPassword] = useState('');
  const [plan, setPlan] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(null);

  const body = (preview) => ({
    agent_id: member.agent_id, successor_agent_id: successor, reason, preview,
    ...(preview ? {} : { password }),
  });

  const run = async (preview) => {
    setBusy(true);
    setError('');
    try {
      const result = await crmPost('/api/privacy/offboard', body(preview));
      if (preview) setPlan(result);
      else if (result?.external_revocation && result.external_revocation !== 'complete') setPending(result);
      else onDone(result);
    } catch (err) {
      setError(friendlyError(err, { fallback: 'Offboarding failed. Nothing was changed.' }));
    } finally {
      setBusy(false);
    }
  };

  if (pending) {
    return (
      <div className={styles.block} aria-label={`Offboard ${member.full_name || member.email}`} role="group">
        <h3 className={styles.heading}>Offboarded {member.full_name || member.email}</h3>
        <p className={styles.text} role="status" data-testid="offboard-pending">
          They are signed out and their open work has moved. {pendingSentence(pending)} Neoh keeps
          trying; if it still cannot, Neoh support is alerted to finish it.
        </p>
        <div className={styles.actions}>
          <button type="button" className={styles.button} onClick={() => onDone(pending)}>Done</button>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.block} aria-label={`Offboard ${member.full_name || member.email}`} role="group">
      <h3 className={styles.heading}>Offboard {member.full_name || member.email}</h3>
      {successors.length === 0 ? (
        <p className={styles.text}>Invite or reinstate someone first — their work needs a new owner.</p>
      ) : (
        <>
          <label className={styles.field}>
            <span className={styles.micro}>Hand their open work to</span>
            <select className={styles.input} value={successor}
              onChange={(e) => { setSuccessor(e.target.value); setPlan(null); }}>
              {successors.map((m) => (
                <option key={m.id} value={m.agent_id}>{m.full_name || m.email}</option>
              ))}
            </select>
          </label>
          <label className={styles.field}>
            <span className={styles.micro}>Reason</span>
            <input className={styles.input} value={reason}
              onChange={(e) => { setReason(e.target.value); setPlan(null); }} />
          </label>
          {plan && (
            <div className={styles.text} role="status" data-testid="offboard-plan">
              <p className={styles.text}>
                Moves to {plan.successor?.agent_id}: {summarise(plan.reassign) || 'no open work'}.
              </p>
              <p className={styles.text}>
                Cancels unsent work: {summarise(plan.cancel_pending) || 'none'}.
                {plan.telephony === 'moved_to_successor' && ' Their business number moves to the new owner.'}
                {plan.telephony === 'parked_successor_has_own_route' && ' The new owner already has a number, so theirs stops taking calls and texts. It is not released; ask Neoh support to move or release it.'}
              </p>
              <p className={styles.dim}>Notes, messages and history they wrote keep their name.</p>
              {(plan.warnings || []).map((w) => <p key={w} className={styles.dim}>{w}</p>)}
              <label className={styles.field}>
                <span className={styles.micro}>Your password</span>
                <input type="password" className={styles.input} autoComplete="current-password"
                  value={password} onChange={(e) => setPassword(e.target.value)} />
              </label>
            </div>
          )}
          {error && <p className={styles.error} role="alert">{error}</p>}
          <div className={styles.actions}>
            <button type="button" className={styles.ghost} onClick={onCancel} disabled={busy}>Cancel</button>
            {!plan ? (
              <button type="button" className={styles.button}
                disabled={busy || !successor || reason.trim().length < 3}
                onClick={() => run(true)}>
                Preview
              </button>
            ) : (
              <button type="button" className={styles.danger} disabled={busy || !password}
                onClick={() => run(false)}>
                Offboard
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

export default OffboardMemberForm;
