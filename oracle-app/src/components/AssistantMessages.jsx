import styles from './AssistantShell.module.css';

function timeLabel(value) {
  const date = new Date(value || Date.now());
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function ActionReceipt({ action, onUndo, undoing }) {
  const fields = Object.entries(action.fields || {});
  const canUndo = action.status !== 'undone';
  return (
    <div className={styles.actionReceipt}>
      <span className={styles.actionMark} aria-hidden="true">✓</span>
      <div>
        <strong>{action.status === 'undone' ? 'Change undone' : 'Record updated'}</strong>
        <small>{fields.map(([key, value]) => `${key.replaceAll('_', ' ')}: ${value ?? 'cleared'}`).join(' · ')}</small>
      </div>
      {canUndo && (
        <button type="button" onClick={() => onUndo(action)} disabled={undoing === action.action_id}>
          {undoing === action.action_id ? 'Undoing…' : 'Undo'}
        </button>
      )}
    </div>
  );
}

export function AssistantMessages({ messages, onUndo, undoing }) {
  if (!messages.length) {
    return (
      <div className={styles.emptyConversation}>
        <div className={styles.orbitMark} aria-hidden="true"><span /><span /><span /></div>
        <span className={styles.eyebrow}>Private operating channel</span>
        <h2>Work the record, not the interface.</h2>
        <p>Ask NEOH to analyze a deal, read a PDF or photo, summarize a client, or update safe internal fields.</p>
        <div className={styles.promptSeeds} aria-label="Example requests">
          <span>“What am I missing?”</span>
          <span>“Summarize this record”</span>
          <span>“Review the attached PDF”</span>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.messageStack} aria-live="polite" aria-relevant="additions text">
      {messages.map((message) => (
        <article
          key={message.id || `${message.request_id}:${message.role}`}
          className={styles.message}
          data-role={message.role}
          data-status={message.status}
        >
          <div className={styles.messageMeta}>
            <span>{message.role === 'user' ? 'You' : 'NEOH'}</span>
            <time dateTime={message.created_at}>{timeLabel(message.created_at)}</time>
          </div>
          <div className={styles.bubble}>
            {message.content ? <p>{message.content}</p> : (
              <span className={styles.thinking} aria-label="NEOH is thinking"><i /><i /><i /></span>
            )}
            {(message.attachments || []).length > 0 && (
              <div className={styles.messageFiles}>
                {message.attachments.map((file) => <span key={file.id}>⌑ {file.filename}</span>)}
              </div>
            )}
          </div>
          {(message.actions || []).map((action) => (
            <ActionReceipt key={action.action_id} action={action} onUndo={onUndo} undoing={undoing} />
          ))}
        </article>
      ))}
    </div>
  );
}
