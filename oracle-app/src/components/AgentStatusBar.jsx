import { useOracleState } from '../state';
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
  const { activeAgent } = useOracleState();
  const activePhase = resolveActivePhase(activeAgent);

  return (
    <div className={styles.bar}>
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
      </div>
    </div>
  );
}
