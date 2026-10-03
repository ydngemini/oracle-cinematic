import { AlertTriangle, CheckCircle2, Clock, MinusCircle, XCircle } from 'lucide-react';

import { commandReceipt, receiptTitle } from '../neoh/actionLabels';
import styles from './AssistantShell.module.css';

function timeLabel(value) {
  const date = new Date(value || Date.now());
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function ActionReceipt({ action, onUndo, undoing }) {
  const fields = Object.entries(action.fields || {});
  const undone = action.status === 'undone';
  // Every one of these three has to hold. The button used to render on `status`
  // alone, so the six tools that wrote no ledger row produced an Undo that
  // POSTed to /api/ai/chat/actions/undefined/undo.
  const canUndo = !undone && action.undoable !== false && Boolean(action.action_id);
  const detail = fields.length
    ? fields.map(([key, value]) => `${key.replaceAll('_', ' ')}: ${value ?? 'cleared'}`).join(' · ')
    : action.detail;
  return (
    <div className={styles.actionReceipt} data-tone={undone ? 'muted' : 'done'}>
      <span className={styles.actionMark} aria-hidden="true">{undone ? '↺' : '✓'}</span>
      <div>
        <strong>{undone ? 'Change undone' : receiptTitle(action)}</strong>
        {detail && <small>{detail}</small>}
        {!undone && action.undoable === false && (
          <small>
            {action.undo_unavailable_reason || 'This change cannot be undone from here.'}
          </small>
        )}
      </div>
      {canUndo && (
        <button type="button" onClick={() => onUndo(action)} disabled={undoing === action.action_id}>
          {undoing === action.action_id ? 'Undoing…' : 'Undo'}
        </button>
      )}
    </div>
  );
}

const TONE_ICONS = {
  done: CheckCircle2, pending: Clock, failed: XCircle, review: AlertTriangle, muted: MinusCircle,
};

/**
 * What became of outreach Neoh staged in this turn — read from the command
 * row itself, so "waiting for approval" turns into "Text sent" or "needs
 * review" in place, durably, instead of the model's sentence being the only
 * evidence. The outcome is always in words; colour and icon only repeat it.
 */
function CommandReceipt({ command, onReview }) {
  const receipt = commandReceipt(command);
  if (!receipt) return null;
  const Icon = TONE_ICONS[receipt.tone] || Clock;
  return (
    <div className={styles.actionReceipt} data-tone={receipt.tone}>
      <span className={styles.actionMark} aria-hidden="true"><Icon size={14} /></span>
      <div>
        <strong>{receipt.title}</strong>
        {receipt.detail && <small>{receipt.detail}</small>}
      </div>
      {receipt.review && onReview && (
        <button type="button" onClick={onReview}>Review</button>
      )}
    </div>
  );
}

export function AssistantMessages({
  messages, onUndo, undoing, receipts = null, onReview, variant = 'panel',
}) {
  if (!messages.length) {
    return (
      <div className={styles.emptyConversation} data-variant={variant}>
        <span className={styles.eyebrow}>Neoh</span>
        <h2>Ask about a person, a property or a deal.</h2>
        <p>Neoh can summarize a client, review a deal, research a property and make safe updates you can undo. Anything that reaches a client waits for your approval.</p>
        <div className={styles.promptSeeds} aria-label="Example requests">
          <span>“What needs me today?”</span>
          <span>“Summarize this client”</span>
          <span>“Who should I call first?”</span>
        </div>
      </div>
    );
  }

  // Not a live region. It streams, and a live region here re-read every
  // chunk; the surface that owns this list announces a finished reply once.
  return (
    <div className={styles.messageStack} data-variant={variant}>
      {messages.map((message) => {
        const staged = receipts && message.role === 'assistant' && message.id
          ? receipts.get(String(message.id).toLowerCase()) || []
          : [];
        return (
          <article
            key={message.id || `${message.request_id}:${message.role}`}
            className={styles.message}
            data-role={message.role}
            data-status={message.status}
            aria-label={message.role === 'user' ? 'You said' : 'Neoh said'}
          >
            <div className={styles.messageMeta}>
              <span>{message.role === 'user' ? 'You' : 'Neoh'}</span>
              <time dateTime={message.created_at}>{timeLabel(message.created_at)}</time>
            </div>
            <div className={styles.bubble}>
              {message.content ? <p>{message.content}</p> : (
                <span className={styles.thinking} role="img" aria-label="Neoh is thinking"><i /><i /><i /></span>
              )}
              {(message.attachments || []).length > 0 && (
                <div className={styles.messageFiles}>
                  {message.attachments.map((file) => <span key={file.id}>⌑ {file.filename}</span>)}
                </div>
              )}
            </div>
            {(message.actions || []).map((action, index) => (
              <ActionReceipt key={action.action_id || `${message.id}-a${index}`} action={action} onUndo={onUndo} undoing={undoing} />
            ))}
            {staged.map((command) => (
              <CommandReceipt key={command.id} command={command} onReview={onReview} />
            ))}
          </article>
        );
      })}
    </div>
  );
}
