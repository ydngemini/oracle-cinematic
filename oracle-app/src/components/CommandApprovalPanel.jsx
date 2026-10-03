import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost, crmPut } from '../state/useCrmApi';
import { useOracleState } from '../state';
import styles from './CommandApprovalPanel.module.css';
import { friendlyError } from '../lib/errorMessages';

// What each approval is, in words — the API's command_type stays internal.
const COMMAND_LABELS = { EMAIL: 'Email', SMS: 'Text message', CALL: 'Call', CALENDAR: 'Calendar event' };
// platform_policy.ActionRisk values → what the approval means for the agent.
const RISK_LABELS = {
  read_only: 'Read only',
  internal_edit: 'Changes your CRM',
  outreach: 'Contacts a client',
  live_call: 'Places a call',
  calendar_write: 'Books your calendar',
  financial: 'Involves money',
  bidding_message: 'Part of an offer',
  legal_document: 'Legal document',
};
const OFFER_GUIDANCE = {
  green: 'Within your limit',
  amber: 'Close to your limit',
  red: 'Over your maximum',
};

function commandLabel(command) {
  return COMMAND_LABELS[command.command_type] || 'Action';
}

function commandSummary(command) {
  if (command.command_type === 'EMAIL') return command.draft?.content?.subject || 'Email draft';
  if (command.command_type === 'CALL') return command.target?.phone || 'Call';
  return command.draft?.content?.event?.summary || command.draft?.content?.body || 'Draft ready for review';
}

export function CommandApprovalPanel() {
  const oracle = useOracleState();
  const [commands, setCommands] = useState(null);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(null);
  const [target, setTarget] = useState('');
  const [draft, setDraft] = useState('');
  const [reason, setReason] = useState('Reviewed target, content, timing, and compliance context.');
  const [busy, setBusy] = useState('');

  const load = useCallback(() => crmGet('/api/commands?limit=50').then(
    (data) => { setCommands(Array.isArray(data?.commands) ? data.commands : []); setError(''); },
    (failure) => setError(friendlyError(failure, { fallback: 'Approvals couldn’t load. Try again in a moment.' })),
  ), []);

  useEffect(() => { load(); }, [load]);

  const decide = (command, decision) => {
    setBusy(command.id);
    crmPost(`/api/commands/${command.id}/${decision}`, { reason: reason.trim() })
      .then(load).catch((failure) => setError(friendlyError(failure, { fallback: 'That decision didn’t save. Try again.' })))
      .finally(() => setBusy(''));
  };

  const beginEdit = (command) => {
    setEditing(command.id);
    setTarget(JSON.stringify(command.target || {}, null, 2));
    setDraft(JSON.stringify(command.draft?.content || {}, null, 2));
    setError('');
  };

  const saveEdit = (command) => {
    let parsedTarget;
    let parsedDraft;
    try {
      parsedTarget = JSON.parse(target);
      parsedDraft = JSON.parse(draft);
    } catch {
      setError('Those details aren’t valid JSON. Check the brackets and quotes, then save again.');
      return;
    }
    setBusy(command.id);
    crmPut(`/api/commands/${command.id}`, {
      target: parsedTarget,
      draft: parsedDraft,
      context: command.draft?.context || {},
      scheduled_at: command.scheduled_at || null,
      approval_expires_minutes: 1440,
    }).then(() => { setEditing(null); return load(); })
      .catch((failure) => setError(friendlyError(failure, { fallback: 'The draft didn’t save. Try again.' })))
      .finally(() => setBusy(''));
  };

  const pending = (commands || []).filter((command) => command.state === 'awaiting_approval');
  const reconciliation = (commands || []).filter((command) => command.state === 'reconciliation_required');

  return (
    <section className={styles.wrap} aria-labelledby="command-approval-title">
      <button type="button" className={styles.summary} onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
        <span><strong id="command-approval-title">Waiting for your approval</strong><small>Emails, calls, and calendar events Neoh has drafted</small></span>
        <span className={styles.count} aria-label={`${pending.length} waiting`}>{pending.length}</span>
      </button>
      {expanded && (
        <div className={styles.body}>
          <p className={styles.policy}>Neoh only sends, calls, or books after you approve. If we can’t confirm something went out, it is never retried on its own.</p>
          {error && <p className={styles.error} role="alert">{error}</p>}
          {reconciliation.length > 0 && (
            <p className={styles.reconcile} role="status">
              {reconciliation.length} {reconciliation.length === 1 ? 'item needs' : 'items need'} review — we couldn’t confirm {reconciliation.length === 1 ? 'it was' : 'they were'} sent, so {reconciliation.length === 1 ? 'it won’t' : 'they won’t'} be sent again automatically.
            </p>
          )}
          {oracle.negotiationTelemetry && (
            <section className={styles.negotiation} role="status" aria-live="polite" aria-atomic="true" data-threshold={oracle.negotiationTelemetry.threshold}>
              <header><strong>Live offer guidance</strong><span>{OFFER_GUIDANCE[oracle.negotiationTelemetry.threshold] || 'Not enough data yet'}</span></header>
              <dl>
                <div><dt>Their counter</dt><dd>${Number(oracle.negotiationTelemetry.counter_offer || 0).toLocaleString()}</dd></div>
                <div><dt>Your max offer</dt><dd>${Number(oracle.negotiationTelemetry.mao || 0).toLocaleString()}</dd></div>
                <div><dt>Stretch limit</dt><dd>${Number(oracle.negotiationTelemetry.amber_max || 0).toLocaleString()}</dd></div>
              </dl>
              <p>{oracle.negotiationTelemetry.objection_draft}</p>
              <small>{oracle.negotiationTelemetry.formula ? `${oracle.negotiationTelemetry.formula} · ` : ''}Facts only — you approve any response.</small>
            </section>
          )}
          <label className={styles.reason}><span>Why you’re approving or rejecting</span><textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={2} minLength={8} maxLength={500} /></label>
          {commands === null ? <div className={styles.skeleton} aria-hidden="true" /> : pending.length === 0 ? <p className={styles.empty}>Nothing is waiting for you. When Neoh drafts an email, call, or event, it appears here for approval.</p> : (
            <ul className={styles.list}>
              {pending.map((command) => (
                <li key={command.id}>
                  <header><strong>{commandLabel(command)}</strong><span>{RISK_LABELS[command.risk_class] || 'Needs review'}</span></header>
                  <p>{commandSummary(command)}</p>
                  {editing === command.id ? (
                    <details className={styles.editor} open>
                      <summary>Advanced: edit the raw draft</summary>
                      <p className={styles.policy}>For support and power users. Saving creates a new draft that still needs approval.</p>
                      <label><span>Recipient details (JSON)</span><textarea value={target} onChange={(event) => setTarget(event.target.value)} rows={5} spellCheck="false" /></label>
                      <label><span>Draft content (JSON)</span><textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={7} spellCheck="false" /></label>
                      <div><button type="button" onClick={() => setEditing(null)}>Cancel</button><button type="button" onClick={() => saveEdit(command)} disabled={busy === command.id}>Save as new draft</button></div>
                    </details>
                  ) : (
                    <footer>
                      <button type="button" onClick={() => beginEdit(command)}>Edit (advanced)</button>
                      <button type="button" onClick={() => decide(command, 'reject')} disabled={busy === command.id || reason.trim().length < 8}>Reject</button>
                      <button type="button" onClick={() => decide(command, 'approve')} disabled={busy === command.id || reason.trim().length < 8}>Approve</button>
                    </footer>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
