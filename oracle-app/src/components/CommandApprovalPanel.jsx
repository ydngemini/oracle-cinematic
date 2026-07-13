import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost, crmPut } from '../state/useCrmApi';
import { useOracleState } from '../state';
import styles from './CommandApprovalPanel.module.css';

function human(value) { return String(value || 'unknown').replaceAll('_', ' '); }

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
    (failure) => setError(failure.message || 'Command approvals are unavailable.'),
  ), []);

  useEffect(() => { load(); }, [load]);

  const decide = (command, decision) => {
    setBusy(command.id);
    crmPost(`/api/commands/${command.id}/${decision}`, { reason: reason.trim() })
      .then(load).catch((failure) => setError(failure.message || 'Decision failed.'))
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
      setError('Target and draft must be valid JSON objects.');
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
      .catch((failure) => setError(failure.message || 'Draft update failed.'))
      .finally(() => setBusy(''));
  };

  const pending = (commands || []).filter((command) => command.state === 'awaiting_approval');
  const reconciliation = (commands || []).filter((command) => command.state === 'reconciliation_required');

  return (
    <section className={styles.wrap} aria-labelledby="command-approval-title">
      <button type="button" className={styles.summary} onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
        <span><strong id="command-approval-title">Command approvals</strong><small>EMAIL · CALL · CALENDAR</small></span>
        <span className={styles.count}>{pending.length}</span>
      </button>
      {expanded && (
        <div className={styles.body}>
          <p className={styles.policy}>Nothing is sent, dialed, or written to a calendar until its immutable draft is approved. Provider-uncertain work is never retried automatically.</p>
          {error && <p className={styles.error} role="alert">{error}</p>}
          {reconciliation.length > 0 && <p className={styles.reconcile} role="alert">{reconciliation.length} command{reconciliation.length === 1 ? '' : 's'} require provider reconciliation; duplicate submission is suppressed.</p>}
          {oracle.negotiationTelemetry && (
            <section className={styles.negotiation} aria-live="polite" data-threshold={oracle.negotiationTelemetry.threshold}>
              <header><strong>Live MAO assistance</strong><span>{human(oracle.negotiationTelemetry.threshold)}</span></header>
              <dl>
                <div><dt>Counter</dt><dd>${Number(oracle.negotiationTelemetry.counter_offer || 0).toLocaleString()}</dd></div>
                <div><dt>MAO</dt><dd>${Number(oracle.negotiationTelemetry.mao || 0).toLocaleString()}</dd></div>
                <div><dt>Amber max</dt><dd>${Number(oracle.negotiationTelemetry.amber_max || 0).toLocaleString()}</dd></div>
              </dl>
              <p>{oracle.negotiationTelemetry.objection_draft}</p>
              <small>{oracle.negotiationTelemetry.formula} · factual inputs only · response requires approval</small>
            </section>
          )}
          <label className={styles.reason}><span>Approval reason</span><textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={2} minLength={8} maxLength={500} /></label>
          {commands === null ? <div className={styles.skeleton} aria-hidden="true" /> : pending.length === 0 ? <p className={styles.empty}>No editable commands awaiting approval.</p> : (
            <ul className={styles.list}>
              {pending.map((command) => (
                <li key={command.id}>
                  <header><strong>{command.command_type}</strong><span>{human(command.risk_class)}</span></header>
                  <p>{command.command_type === 'EMAIL' ? command.draft?.content?.subject : command.command_type === 'CALL' ? command.target?.phone : command.draft?.content?.event?.summary || 'Draft command'}</p>
                  {editing === command.id ? (
                    <div className={styles.editor}>
                      <label><span>Target JSON</span><textarea value={target} onChange={(event) => setTarget(event.target.value)} rows={5} spellCheck="false" /></label>
                      <label><span>Draft JSON</span><textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={7} spellCheck="false" /></label>
                      <div><button type="button" onClick={() => setEditing(null)}>Cancel</button><button type="button" onClick={() => saveEdit(command)} disabled={busy === command.id}>Save new approval draft</button></div>
                    </div>
                  ) : (
                    <footer>
                      <button type="button" onClick={() => beginEdit(command)}>Edit</button>
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
