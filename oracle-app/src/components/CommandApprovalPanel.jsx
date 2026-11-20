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

// External audit 26.5: the person approving a one-to-one text says what it is
// for. Marketing (preselected) needs documented written consent; informational
// needs the person's consent to be texted. The server decides what each needs
// and refuses informational for campaign texts — this only asks.
const PURPOSE_OPTIONS = [
  ['marketing', 'Marketing', 'Promotes a listing, your services or an offer. Needs documented written consent.'],
  ['informational', 'Informational',
    'A reply to them, a showing or appointment confirmation, or a document or deadline reminder, with no promotion. Needs their consent to be texted.'],
];
// Texts a pipeline wrote for a campaign stay marketing (mirrors the server).
const CAMPAIGN_SOURCES = {
  'speed-to-lead': 'speed-to-lead',
  'smart-plan': 'a sales sequence',
  'our-ai-sales-agent': 'the AI sales agent',
};

function campaignOrigin(command) {
  const context = command.draft?.context || {};
  if (context.mission_id || String(command.created_by || '').startsWith('mission:')) return 'a mission';
  return CAMPAIGN_SOURCES[context.source] || null;
}

function commandLabel(command) {
  return COMMAND_LABELS[command.command_type] || 'Action';
}

function commandSummary(command) {
  if (command.command_type === 'EMAIL') return command.draft?.content?.subject || 'Email draft';
  if (command.command_type === 'CALL') {
    // What the voice assistant will be told, so the approver sees it (34.3).
    const draft = command.draft?.content || {};
    return [command.target?.phone, draft.reason, draft.objective, draft.property_address]
      .filter(Boolean).join(' · ') || 'Call';
  }
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
  const [purposes, setPurposes] = useState({});

  const load = useCallback(() => crmGet('/api/commands?limit=50').then(
    (data) => { setCommands(Array.isArray(data?.commands) ? data.commands : []); setError(''); },
    (failure) => setError(friendlyError(failure, { fallback: 'Approvals couldn’t load. Try again in a moment.' })),
  ), []);

  useEffect(() => { load(); }, [load]);

  const decide = (command, decision) => {
    setBusy(command.id);
    const body = { reason: reason.trim() };
    if (decision === 'approve' && command.command_type === 'SMS' && !campaignOrigin(command)) {
      body.purpose = purposes[command.id] || 'marketing';
    }
    crmPost(`/api/commands/${command.id}/${decision}`, body)
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
                  {command.command_type === 'SMS' && campaignOrigin(command) ? (
                    <p className={styles.purposeNote}>
                      This text comes from {campaignOrigin(command)}, so it is treated as marketing: it needs documented written consent.
                    </p>
                  ) : null}
                  {command.command_type === 'SMS' && !campaignOrigin(command) ? (
                    <fieldset className={styles.purpose}>
                      <legend>What is this text for?</legend>
                      {PURPOSE_OPTIONS.map(([value, label, hint]) => (
                        <label key={value}>
                          <input
                            type="radio"
                            name={`purpose-${command.id}`}
                            value={value}
                            checked={(purposes[command.id] || 'marketing') === value}
                            onChange={() => setPurposes((current) => ({ ...current, [command.id]: value }))}
                          />
                          <span><strong>{label}</strong><small>{hint}</small></span>
                        </label>
                      ))}
                    </fieldset>
                  ) : null}
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
