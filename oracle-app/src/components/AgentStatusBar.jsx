import { AlertTriangle, CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { useOptionalAssistant } from './AssistantContext';
import styles from './AgentStatusBar.module.css';

// Each state carries an icon AND words, so the outcome never rests on colour.
const STATE_META = {
  analyzing: { Icon: Loader2, fallback: 'Neoh is working on it…', spin: true },
  authorizing: { Icon: Loader2, fallback: 'Checking your approval…', spin: true },
  queued: { Icon: CheckCircle2, fallback: 'Queued — Neoh will follow up here.' },
  completed: { Icon: CheckCircle2, fallback: 'Done.' },
  cancelled: { Icon: XCircle, fallback: 'Cancelled.' },
  failed: { Icon: AlertTriangle, fallback: "Neoh couldn't finish that. Nothing was sent." },
};

/**
 * Calm status line for the request Neoh is handling. It renders nothing while
 * idle — the old idle state was a row of fake pipeline labels (SCOUTING_MATRIX,
 * MEMORY SYNC) that described no real work.
 */
export function AgentStatusBar() {
  const assistant = useOptionalAssistant();
  const commandStatus = assistant?.commandStatus;
  if (!commandStatus || commandStatus.state === 'idle') return null;

  const meta = STATE_META[commandStatus.state] || STATE_META.analyzing;
  const { Icon } = meta;
  return (
    <div
      className={styles.commandBar}
      data-state={commandStatus.state}
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <Icon
        className={`${styles.commandIcon} ${meta.spin ? styles.spin : ''}`}
        size={16}
        aria-hidden="true"
      />
      <span className={styles.commandCopy}>
        <strong>{commandStatus.message || meta.fallback}</strong>
        {commandStatus.detail && <small>{commandStatus.detail}</small>}
      </span>
    </div>
  );
}
