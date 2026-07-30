import { useOracleState } from '../state';
import { useOptionalAssistant } from './AssistantContext';
import { BorderBeam } from './motion/BorderBeam';
import { KineticText } from './motion/KineticText';
import styles from './AgentStatusBar.module.css';

const PHASES = [
  { id: 'scout', label: 'SCOUTING_MATRIX' },
  { id: 'stage', label: 'SPATIAL_STAGING' },
  { id: 'voice', label: 'VOICE_NEGOTIATION' },
];

function resolveActivePhase(agent) {
  if (!agent) return null;
  const upper = agent.toUpperCase();
  if (upper.includes('SCOUT') || upper.includes('INGEST') || upper.includes('SCAN')) return 'scout';
  if (upper.includes('DESIGN') || upper.includes('STAGE') || upper.includes('SPATIAL')) return 'stage';
  if (upper.includes('CLOSER') || upper.includes('VOICE') || upper.includes('NEGOTIAT')) return 'voice';
  return 'scout';
}

export function AgentStatusBar() {
  const { activeAgent, memorySync } = useOracleState();
  const assistant = useOptionalAssistant();
  const activePhase = resolveActivePhase(activeAgent);
  const commandStatus = assistant?.commandStatus;

  if (commandStatus && commandStatus.state !== 'idle') {
    const failed = commandStatus.state === 'failed';
    const completed = ['completed', 'queued'].includes(commandStatus.state);
    return (
      <div
        className={`${styles.commandBar} hud-glass-panel hud-reticle`}
        data-state={commandStatus.state}
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        <BorderBeam
          duration={4}
          size={250}
          colorFrom={failed ? '#ef4444' : completed ? '#10b981' : '#b88952'}
          colorTo={failed ? '#f59e0b' : completed ? '#f4e5bc' : '#dfbd73'}
        />
        <span className={styles.commandPulse} aria-hidden="true" />
        <span className={styles.commandCopy}>
          <strong>
            <KineticText
              text={commandStatus.message || 'NEOH is processing'}
              speed={34}
              scrambleSpeed={28}
            />
          </strong>
          {commandStatus.detail && <small>{commandStatus.detail}</small>}
        </span>
      </div>
    );
  }

  return (
    <div className={`${styles.bar} hud-glass-panel hud-reticle`}>
      <div className={styles.phases}>
        {PHASES.map(({ id, label }) => (
          <span
            key={id}
            className={styles.phase}
            data-active={activePhase === id}
          >
            {label}
          </span>
        ))}
        <span className={styles.memory} data-active={memorySync}>
          {memorySync ? 'MEMORY SYNC: ACTIVE' : 'MEMORY SYNC: —'}
        </span>
      </div>
    </div>
  );
}
